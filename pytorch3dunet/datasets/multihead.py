import glob
import os
import copy
from itertools import chain

import h5py
import numpy as np
from scipy import ndimage
import vigra

import pytorch3dunet.augment.transforms as transforms
from pytorch3dunet.datasets.utils import get_slice_builder, ConfigDataset, calculate_stats
from pytorch3dunet.unet3d.utils import get_logger

logger = get_logger('MultiheadDataset')

class MultiheadDataset(ConfigDataset):
    """
    Based on HDF5DataSet for multiple heads use case.
    """

    def __init__(self, file_path,
                 phase,
                 slice_builder_config,
                 transformer_config,
                 mirror_padding=(16, 32, 32),
                 raw_internal_path='raw',
                 label_internal_path='label',
                 weight_internal_path=None):

        assert phase in ['train', 'val', 'test']
        if phase in ['train', 'val']:
            mirror_padding = None

        if mirror_padding is not None:
            if isinstance(mirror_padding, int):
                mirror_padding = (mirror_padding,) * 3
            else:
                assert len(mirror_padding) == 3, f"Invalid mirror_padding: {mirror_padding}"

        self.mirror_padding = mirror_padding
        self.phase = phase
        self.file_path = file_path

        # convert raw_internal_path, label_internal_path and weight_internal_path to list for ease of computation
        if isinstance(raw_internal_path, str):
            raw_internal_path = [raw_internal_path]
        if isinstance(label_internal_path, str):
            label_internal_path = [label_internal_path]
        if isinstance(weight_internal_path, str):
            weight_internal_path = [weight_internal_path]

        internal_paths = list(raw_internal_path)
        if label_internal_path is not None:
            internal_paths.extend(label_internal_path)
        if weight_internal_path is not None:
            internal_paths.extend(weight_internal_path)

        input_file = self.create_h5_file(file_path, internal_paths)

        self.raws = self.fetch_and_check(input_file, raw_internal_path)

        # calculate global min, max, mean and std for normalization
        min_value, max_value, mean, std = calculate_stats(self.raws)
        logger.info(f'Input stats: min={min_value}, max={max_value}, mean={mean}, std={std}')

        self.transformer = transforms.get_transformer(transformer_config, min_value=min_value, max_value=max_value,
                                                      mean=mean, std=std)
        self.raw_transform = self.transformer.raw_transform()

        if phase != 'test':
            # create label/weight transform only in train/val phase
            self.label_transform = self.transformer.label_transform()

            # Fetch labels for segmentation and regression separately
            
            self.labels_seg = self.fetch_and_check(input_file, label_internal_path)
            self.labels_reg = self.fetch_and_check(input_file, label_internal_path)

            if weight_internal_path is not None:
                # look for the weight map in the raw file
                self.weight_maps = self.fetch_and_check(input_file, weight_internal_path)
                self.weight_transform = self.transformer.weight_transform()
            else:
                self.weight_maps = None

            self._check_dimensionality(self.raws, self.labels_seg)
            self._check_dimensionality(self.raws, self.labels_reg)
        else:
            # 'test' phase used only for predictions so ignore the label dataset
            self.labels = None
            self.weight_maps = None

            # add mirror padding if needed
            if self.mirror_padding is not None:
                z, y, x = self.mirror_padding
                pad_width = ((z, z), (y, y), (x, x))
                padded_volumes = []
                for raw in self.raws:
                    if raw.ndim == 4:
                        channels = [np.pad(r, pad_width=pad_width, mode='reflect') for r in raw]
                        padded_volume = np.stack(channels)
                    else:
                        padded_volume = np.pad(raw, pad_width=pad_width, mode='reflect')

                    padded_volumes.append(padded_volume)

                self.raws = padded_volumes

        if phase == 'train' or phase == 'val':

            # Dilate the labels for segmentation

            self.dilation_list = copy.deepcopy(self.labels_seg)
            self.dilated_labels = []
            for each_vol in self.dilation_list:
                self.label_dilate = each_vol
                self.inv_label = np.logical_not(self.label_dilate)
                self.label_dist_transform = ndimage.distance_transform_edt(self.inv_label)
                self.label_thresh_tr = self.label_dist_transform > 2

                self.label_thresh_tr = np.logical_not(self.label_thresh_tr).astype(np.float64)
                self.dilated_labels.append(self.label_thresh_tr)

            self.labels_seg = self.dilated_labels

            # EDT of labels for regression (not including vector edt currently)

            self.edt_list = copy.deepcopy(self.labels_reg)
            self.edt_labels = []
            for each_edt_vol in self.edt_list:
                self.edt_label = vigra.filters.distanceTransform(each_edt_vol[0].astype(np.float32))
                self.edt_label = np.expand_dims(self.edt_label, axis=0)

                #self.vec_edt = vigra.filters.vectorDistanceTransform(each_edt_vol[0].astype(np.float32))
                #self.vec_edt = np.transpose(self.vec_edt, (3,0,1,2))

                #self.new_label = np.concatenate((self.edt_label, self.vec_edt), axis=0)
                #self.edt_labels.append(self.new_label)
                
                self.edt_labels.append(self.edt_label)

            self.labels_reg = self.edt_labels

            # build slice indices for raw and label data sets

        if phase == 'train' or phase == 'val':
            slice_builder_seg = get_slice_builder(self.raws, self.labels_seg, self.weight_maps, slice_builder_config)
            slice_builder_reg = get_slice_builder(self.raws, self.labels_reg, self.weight_maps, slice_builder_config)

        # For testing, get only the raw slices        
        
        slice_builder = get_slice_builder(self.raws, None, self.weight_maps, slice_builder_config)
        self.raw_slices = slice_builder.raw_slices
        
        if phase == 'train' or phase == 'val':
            self.label_slices_seg = slice_builder_seg.label_slices_seg
            self.label_slices_reg = slice_builder_reg.label_slices_reg

        self.weight_slices = slice_builder.weight_slices

        self.patch_count = len(self.raw_slices)
        logger.info(f'Number of patches: {self.patch_count}')

    @staticmethod
    def create_h5_file(file_path, internal_paths):
        raise NotImplementedError

    @staticmethod
    def fetch_datasets(input_file_h5, internal_paths):
        raise NotImplementedError

    def fetch_and_check(self, input_file_h5, internal_paths):
        datasets = self.fetch_datasets(input_file_h5, internal_paths)
        # expand dims if 2d
        fn = lambda ds: np.expand_dims(ds, axis=0) if ds.ndim == 2 else ds
        datasets = list(map(fn, datasets))
        return datasets

    def __getitem__(self, idx):
        if idx >= len(self):
            raise StopIteration

        # get the slice for a given index 'idx'
        raw_idx = self.raw_slices[idx]
        # get the raw data patch for a given slice
        raw_patch_transformed = self._transform_patches(self.raws, raw_idx, self.raw_transform)

        if self.phase == 'test':
            # discard the channel dimension in the slices: predictor requires only the spatial dimensions of the volume
            if len(raw_idx) == 4:
                raw_idx = raw_idx[1:]
            return raw_patch_transformed, raw_idx
        else:
            # get the slice for a given index 'idx'
            label_idx_seg = self.label_slices_seg[idx]
            label_idx_reg = self.label_slices_reg[idx]

            label_patch_transformed_seg = self._transform_patches(self.labels_seg, label_idx_seg, self.label_transform)
            label_patch_transformed_reg = self._transform_patches(self.labels_reg, label_idx_reg, self.label_transform)

            if self.weight_maps is not None:
                weight_idx = self.weight_slices[idx]
                # return the transformed weight map for a given patch together with raw and label data
                weight_patch_transformed = self._transform_patches(self.weight_maps, weight_idx, self.weight_transform)
                return raw_patch_transformed, label_patch_transformed_seg, label_patch_transformed_reg, weight_patch_transformed
            # return the transformed raw and label patches
            return raw_patch_transformed, label_patch_transformed_seg, label_patch_transformed_reg

    @staticmethod
    def _transform_patches(datasets, label_idx, transformer):
        transformed_patches = []
        for dataset in datasets:
            # get the label data and apply the label transformer
            transformed_patch = transformer(dataset[label_idx])
            transformed_patches.append(transformed_patch)

        # if transformed_patches is a singleton list return the first element only
        if len(transformed_patches) == 1:
            return transformed_patches[0]
        else:
            return transformed_patches

    def __len__(self):
        return self.patch_count

    @staticmethod
    def _check_dimensionality(raws, labels):
        def _volume_shape(volume):
            if volume.ndim == 3:
                return volume.shape
            return volume.shape[1:]

        for raw, label in zip(raws, labels):
            assert raw.ndim in [3, 4], 'Raw dataset must be 3D (DxHxW) or 4D (CxDxHxW)'
            assert label.ndim in [3, 4], 'Label dataset must be 3D (DxHxW) or 4D (CxDxHxW)'

            assert _volume_shape(raw) == _volume_shape(label), 'Raw and labels have to be of the same size'

    @classmethod
    def create_datasets(cls, dataset_config, phase):
        phase_config = dataset_config[phase]

        # load data augmentation configuration
        transformer_config = phase_config['transformer']
        # load slice builder config
        slice_builder_config = phase_config['slice_builder']
        # load files to process
        file_paths = phase_config['file_paths']
        # file_paths may contain both files and directories; if the file_path is a directory all H5 files inside
        # are going to be included in the final file_paths
        file_paths = cls.traverse_h5_paths(file_paths)

        datasets = []
        for file_path in file_paths:
            try:
                logger.info(f'Loading {phase} set from: {file_path}...')
                dataset = cls(file_path=file_path,
                              phase=phase,
                              slice_builder_config=slice_builder_config,
                              transformer_config=transformer_config,
                              mirror_padding=dataset_config.get('mirror_padding', None),
                              raw_internal_path=dataset_config.get('raw_internal_path', 'raw'),
                              label_internal_path=dataset_config.get('label_internal_path', 'label'),
                              weight_internal_path=dataset_config.get('weight_internal_path', None))
                datasets.append(dataset)
            except Exception:
                logger.error(f'Skipping {phase} set: {file_path}', exc_info=True)
        return datasets

    @staticmethod
    def traverse_h5_paths(file_paths):
        assert isinstance(file_paths, list)
        results = []
        for file_path in file_paths:
            if os.path.isdir(file_path):
                # if file path is a directory take all H5 files in that directory
                iters = [glob.glob(os.path.join(file_path, ext)) for ext in ['*.h5', '*.hdf', '*.hdf5', '*.hd5']]
                for fp in chain(*iters):
                    results.append(fp)
            else:
                results.append(file_path)
        return results
