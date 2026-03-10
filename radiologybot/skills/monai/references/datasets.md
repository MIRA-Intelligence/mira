# MONAI Datasets

## I/O Bottlenecks
Medical images (like 3D NIfTI files) are vast. Standard PyTorch `Dataset` re-reading from disk creates massive I/O bottlenecks.

## Core Dataset Offerings
- **Dataset**: Vanilla lazy loading. Slow. Use only for inference/testing.
- **CacheDataset**: Pre-computes all non-random transforms and caches the volume in RAM. Essential for fast training. Use `num_workers` to speed up caching.
- **PersistentDataset**: Caches pre-computed transforms to a specified disk directory. Best for datasets that are too large for RAM.
- **SmartCacheDataset**: Drops and replaces items in the cache asynchronously during training.

## DataLoader
When creating the `DataLoader`, use MONAI's memory-pinned formats or `list_data_collate` to deal with dictionaries correctly.
