# -*- coding: utf-8 -*-
"""
Batched data loading with tensorclasses
=======================================
"""

##############################################################################
# In this tutorial we demonstrate how tensorclasses and memory-mapped
# tensors can be used together to efficiently and transparently load data
# from disk inside a model training pipeline.
#
# The basic idea is that we pre-load the entire dataset into a
# memory-mapped tensors, applying any non-random transformations before
# saving to disk. This means that not only do we avoid performing repeated
# computation each time we iterate through the data, we also are able to
# efficiently load data from the memory-mapped tensor in batches, rather
# than sequentially from the raw image files.
#
# Using the combination of pre-processing, loading on a contiguous physical-memory
# storage and on-device batched transformation, we obtain a 10x speedup in data-loading
# over regular torch + torchvision pipelines.
#
# We’ll use the same subset of imagenet used in `this transfer learning
# tutorial <https://pytorch.org/tutorials/beginner/transfer_learning_tutorial.html>`__,
# though we also give results of our experiments running the same code on ImageNet.
#
# .. note:: Download the data from
#   `here <https://download.pytorch.org/tutorial/hymenoptera_data.zip>`__
#   and extract it. We assume in this tutorial that the extracted data is
#   saved in the subdirectory ``data/``.
#
import os
import time
from pathlib import Path

import torch
import torch.nn as nn
import tqdm
from tensordict import MemmapTensor
from tensordict.prototype import tensorclass
from torch import multiprocessing as mp
from torch.utils.data import DataLoader
from torchvision import datasets, transforms

NUM_WORKERS = int(os.environ.get("NUM_WORKERS", "4"))
# sphinx_gallery_start_ignore
# this example can be run locally or in the tensordict CI on the small hymenoptera
# subset of imagenet, but we also want to be able to compare to runs on larger subsets
# of imagenet which would be impractical to run in CI. If this script is run with the
# environment variable RUN_ON_CLUSTER set, then we set everything to run on a larger
# subset of imagenet. the fraction of images can be set with the FRACTION environment
# variable, we use the first `len(dataset) // FRACTION` images. Default is 10.
RUN_ON_CLUSTER = os.environ.get("RUN_ON_CLUSTER", False)
if RUN_ON_CLUSTER in ("0", "False", False):
    RUN_ON_CLUSTER = False
elif RUN_ON_CLUSTER in ("1", "True", True):
    RUN_ON_CLUSTER = True
else:
    raise NotImplementedError(RUN_ON_CLUSTER)
FRACTION = int(os.environ.get("FRACTION", 10))
# sphinx_gallery_end_ignore
if torch.cuda.is_available():
    # If the data is collected on cuda, we must use the "spawn" multiprocessing
    # mode, as it is the only one compatible with cuda-shared tensors.
    mp.set_start_method("spawn")
    device = "cuda:0"
else:
    device = "cpu"

print(f"Using device: {device}")


##############################################################################
# Transforms
# ----------
# First we define train and val transforms that will be applied to train and
# val examples respectively. Note that there are random components in the
# train transform to prevent overfitting to training data over multiple
# epochs.

train_transform = transforms.Compose(
    [
        transforms.RandomResizedCrop(224),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ]
)

val_transform = transforms.Compose(
    [
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ]
)

##############################################################################
# We use ``torchvision.datasets.ImageFolder`` to conveniently load and
# transform the data from disk.

data_dir = Path("data/hymenoptera_data/")
# sphinx_gallery_start_ignore
if RUN_ON_CLUSTER:
    data_dir = Path("/datasets01_ontap/imagenet_full_size/061417/")
# sphinx_gallery_end_ignore

train_data = datasets.ImageFolder(root=data_dir / "train", transform=train_transform)
val_data = datasets.ImageFolder(root=data_dir / "val", transform=val_transform)
# sphinx_gallery_start_ignore
if RUN_ON_CLUSTER:
    if FRACTION > 1:
        train_data.samples = train_data.samples[: len(train_data) // FRACTION]
        val_data.samples = val_data.samples[: len(val_data) // FRACTION]
# sphinx_gallery_end_ignore

##############################################################################
# We’ll also create a dataset of the raw training data that simply resizes
# the image to a common size and converts to tensor. We’ll use this to
# load the data into memory-mapped tensors. The random transformations
# need to be different each time we iterate through the data, so they
# cannot be pre-computed. We also do not scale the data yet so that we can set the
# ``dtype`` of the memory-mapped array to ``uint8`` and save space.

train_data_raw = datasets.ImageFolder(
    root=data_dir / "train",
    transform=transforms.Compose(
        [transforms.Resize((256, 256)), transforms.PILToTensor()]
    ),
)
# sphinx_gallery_start_ignore
if RUN_ON_CLUSTER:
    train_data_raw.samples = train_data_raw.samples[: len(train_data_raw) // FRACTION]
# sphinx_gallery_end_ignore


##############################################################################
# Since we'll be loading our data in batches, we write a few custom transformations
# that take advantage of this, and apply the transformations in a vectorized way.
#
# First a transformation that can be used for normalization.
class InvAffine(nn.Module):
    """A custom normalization layer."""

    def __init__(self, loc, scale):
        super().__init__()
        self.loc = loc
        self.scale = scale

    def forward(self, x):
        return (x - self.loc) / self.scale


##############################################################################
# Next two transformations that can be used to randomly crop and flip the images.


class RandomHFlip(nn.Module):
    def forward(self, x: torch.Tensor):
        idx = (
            torch.zeros(*x.shape[:-3], 1, 1, 1, device=x.device, dtype=torch.bool)
            .bernoulli_()
            .expand_as(x)
        )
        return x.masked_fill(idx, 0.0) + x.masked_fill(~idx, 0.0).flip(-1)


class RandomCrop(nn.Module):
    def __init__(self, w, h):
        super(RandomCrop, self).__init__()
        self.w = w
        self.h = h

    def forward(self, x):
        batch = x.shape[:-3]
        index0 = torch.randint(x.shape[-2] - self.h, (*batch, 1), device=x.device)
        index0 = index0 + torch.arange(self.h, device=x.device)
        index0 = (
            index0.unsqueeze(1).unsqueeze(-1).expand(*batch, 3, self.h, x.shape[-1])
        )
        index1 = torch.randint(x.shape[-1] - self.w, (*batch, 1), device=x.device)
        index1 = index1 + torch.arange(self.w, device=x.device)
        index1 = index1.unsqueeze(1).unsqueeze(-2).expand(*batch, 3, self.h, self.w)
        return x.gather(-2, index0).gather(-1, index1)


##############################################################################
# When each batch is loaded, we will scale it, then randomly crop and flip. The random
# transformations cannot be pre-applied as they must differ each time we iterate over
# the data. The scaling could be pre-applied in principle, but by waiting until we load
# the data into RAM, we are able to set the dtype of the memory-mapped array to
# ``uint8``, a significant space saving over ``float32``.

collate_transform = nn.Sequential(
    InvAffine(
        loc=torch.tensor([0.485, 0.456, 0.406], device=device).view(3, 1, 1) * 255,
        scale=torch.tensor([0.229, 0.224, 0.225], device=device).view(3, 1, 1) * 255,
    ),
    RandomCrop(224, 224),
    RandomHFlip(),
)

##############################################################################
# Representing data with a TensorClass
# ------------------------------------
# Tensorclasses are a good choice when the structure of your data is known
# apriori. They are dataclasses that expose dedicated tensor methods over
# their contents much like a ``TensorDict``.
#
# As well as specifying the contents (in this case ``images`` and
# ``targets``) we can also encapsulate related logic as custom methods
# when defining the class. Here we add a classmethod that takes a dataset
# and creates a tensorclass containing the data by iterating over the
# dataset. We create memory-mapped tensors to hold the data so that they
# can be efficiently loaded in batches later.


@tensorclass
class ImageNetData:
    images: torch.Tensor
    targets: torch.Tensor

    @classmethod
    def from_dataset(cls, dataset):
        data = cls(
            images=MemmapTensor(
                len(dataset),
                *dataset[0][0].squeeze().shape,
                dtype=torch.uint8,
            ),
            targets=MemmapTensor(len(dataset), dtype=torch.int64),
            batch_size=[len(dataset)],
        )
        data = data.memmap_()

        batch = 64
        dl = DataLoader(dataset, batch_size=batch, num_workers=NUM_WORKERS)
        i = 0
        pbar = tqdm.tqdm(total=len(dataset))
        for image, target in dl:
            _batch = image.shape[0]
            pbar.update(_batch)
            data[i : i + _batch] = cls(
                images=image, targets=target, batch_size=[_batch]
            )
            i += _batch

        return data


##############################################################################
# We create two tensorclasses, one for the training and on for the
# validation data. Note that while this step can be slightly expensive, it
# allows us to save repeated computation later during training.

train_data_tc = ImageNetData.from_dataset(train_data_raw)
val_data_tc = ImageNetData.from_dataset(val_data)

##############################################################################
# DataLoaders
# -----------
#
# We can create dataloaders both from the ``torchvision``-provided
# Datasets, as well as from our memory-mapped tensorclasses.
#
# Since tensorclasses implement ``__len__`` and ``__getitem__`` (and also
# ``__getitems__``) we can use them like a map-style Dataset and create a
# ``DataLoader`` directly from them.
#
# Since the TensorClass data will be loaded in batches, we need to specify how these
# batches should be collated. For this we write the following helper class


class Collate(nn.Module):
    def __init__(self, transform=None, device=None):
        super().__init__()
        self.transform = transform
        self.device = device

    def __call__(self, x: ImageNetData):
        # move data to RAM
        out = x.apply(lambda x: x.contiguous())
        if self.device:
            # move data to gpu
            out = out.to(self.device)
        if self.transform:
            # apply transforms on gpu
            out.images = self.transform(out.images)
        return out


##############################################################################
# ``DataLoader`` has support for multiple workers loading data in parallel. The
# tensorclass dataloader will use just one worker, but load data in batches.

batch_size = 8
# sphinx_gallery_start_ignore
if RUN_ON_CLUSTER:
    batch_size = 128
# sphinx_gallery_end_ignore
train_dataloader = DataLoader(
    train_data,
    batch_size=batch_size,
    num_workers=NUM_WORKERS,
)
val_dataloader = DataLoader(
    val_data,
    batch_size=batch_size,
    num_workers=NUM_WORKERS,
)

train_dataloader_tc = DataLoader(
    train_data_tc,
    batch_size=batch_size,
    collate_fn=Collate(collate_transform, device),
    num_workers=4,
)
val_dataloader_tc = DataLoader(
    val_data_tc,
    batch_size=batch_size,
    collate_fn=Collate(device=device),
    num_workers=4,
)

##############################################################################
# We can now compare how long it takes to iterate once over the data in
# each case. The regular dataloader loads images one by one from disk,
# applies the transform sequentially and then stacks the results
# (note: we start measuring time a little after the first iteration, as
# starting the dataloader can take some time).

total = 0
for i, (image, target) in enumerate(train_dataloader):
    if i == 3:
        t0 = time.time()
    if i >= 3:
        total += image.shape[0]
    image, target = image.to(device), target.to(device)
t = time.time() - t0
print(f"One iteration over dataloader done! Rate: {total/t:4.4f} fps, time: {t: 4.4f}s")

##############################################################################
# Our tensorclass-based dataloader instead loads data from the
# memory-mapped tensor in batches. We then apply the batched random
# transformations to the batched images.

total = 0
for i, batch in enumerate(train_dataloader_tc):
    if i == 3:
        t0 = time.time()
    if i >= 3:
        total += batch.numel()
    image, target = batch.images, batch.targets
t = time.time() - t0
print(
    f"One iteration over tensorclass dataloader done! Rate: {total/t:4.4f} fps, time: {t: 4.4f}s"
)

##############################################################################
# In the case of the validation set, we see an even bigger performance
# improvement, because there are no random transformations, so we can save
# the fully transformed data in the memory-mapped tensor, eliminating the
# need for additional transformations as we load from disk.

total = 0
for i, (image, target) in enumerate(val_dataloader):
    if i == 3:
        t0 = time.time()
    if i >= 3:
        total += image.shape[0]
    image, target = image.to(device), target.to(device)
t = time.time() - t0
print(f"One iteration over val data done! Rate: {total/t:4.4f} fps, time: {t: 4.4f}s")

total = 0
for i, batch in enumerate(val_dataloader_tc):
    if i == 3:
        t0 = time.time()
    if i >= 3:
        total += batch.shape[0]
    image, target = batch.images.contiguous().to(device), batch.targets.contiguous().to(
        device
    )
t = time.time() - t0
print(
    f"One iteration over tensorclass val data done! Rate: {total/t:4.4f} fps, time: {t: 4.4f}s"
)

##############################################################################
# Results from ImageNet
# ---------------------
#
# Repeating the above on full-size ImageNet data, we get the following results
#
#  | One iteration over dataloader done! Rate: 2984.8455 fps, time:  428.7951s
#  | One iteration over tensorclass dataloader done! Rate: 6457.9865 fps, time:  198.1867s
#  | One iteration over val data done! Rate: 2444.2872 fps, time:  19.9322s
#  | One iteration over tensorclass val data done! Rate: 8813.6024 fps, time:  5.5278s
#
# This shows that much of the overhead is coming from i/o operations rather than the
# transforms, and hence explains how the memory-mapped array helps us load data more
# efficiently.
