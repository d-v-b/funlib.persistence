from .array import Array

from funlib.geometry import Coordinate, Roi

import zarr
import h5py

import json
import logging
import os
import shutil
from typing import Optional, Union

logger = logging.getLogger(__name__)


def _read_voxel_size_offset(ds, order="C"):
    voxel_size = None
    offset = None
    dims = None

    if "resolution" in ds.attrs:
        voxel_size = Coordinate(ds.attrs["resolution"])
        dims = len(voxel_size)
    elif "scale" in ds.attrs:
        voxel_size = Coordinate(ds.attrs["scale"])
        dims = len(voxel_size)
    elif "pixelResolution" in ds.attrs:
        voxel_size = Coordinate(ds.attrs["pixelResolution"]["dimensions"])
        dims = len(voxel_size)

    elif "transform" in ds.attrs:
        # Davis saves transforms in C order regardless of underlying
        # memory format (i.e. n5 or zarr). May be explicitly provided
        # as transform.ordering
        transform_order = ds.attrs["transform"].get("ordering", "C")
        voxel_size = Coordinate(ds.attrs["transform"]["scale"])
        if transform_order != order:
            voxel_size = Coordinate(voxel_size[::-1])
        dims = len(voxel_size)

    if "offset" in ds.attrs:
        offset = Coordinate(ds.attrs["offset"])
        if dims is not None:
            assert dims == len(
                offset
            ), "resolution and offset attributes differ in length"
        else:
            dims = len(offset)

    elif "transform" in ds.attrs:
        transform_order = ds.attrs["transform"].get("ordering", "C")
        offset = Coordinate(ds.attrs["transform"]["translate"])
        if transform_order != order:
            offset = Coordinate(offset[::-1])

    if dims is None:
        dims = len(ds.shape)

    if voxel_size is None:
        voxel_size = Coordinate((1,) * dims)

    if offset is None:
        offset = Coordinate((0,) * dims)

    if order == "F":
        offset = Coordinate(offset[::-1])
        voxel_size = Coordinate(voxel_size[::-1])

    if voxel_size is not None and (offset / voxel_size) * voxel_size != offset:
        # offset is not a multiple of voxel_size. This is often due to someone defining
        # offset to the point source of each array element i.e. the center of the rendered
        # voxel, vs the offset to the corner of the voxel.
        # apparently this can be a heated discussion. See here for arguments against
        # the convention we are using: http://alvyray.com/Memos/CG/Microsoft/6_pixel.pdf
        logger.debug(
            f"Offset: {offset} being rounded to nearest voxel size: {voxel_size}"
        )
        offset = (
            (Coordinate(offset) + (Coordinate(voxel_size) / 2)) / Coordinate(voxel_size)
        ) * Coordinate(voxel_size)
        logger.debug(f"Rounded offset: {offset}")

    return Coordinate(voxel_size), Coordinate(offset)


def open_ds(filename: str, ds_name: str, mode: str = "r") -> Array:
    """Open a Zarr, N5, or HDF5 dataset as an :class:`Array`. If the
    dataset has attributes ``resolution`` and ``offset``, those will be
    used to determine the meta-information of the returned array.

    Args:

        filename:

            The name of the container "file" (which is a directory for Zarr and
            N5).

        ds_name:

            The name of the dataset to open.

    Returns:

        A :class:`Array` pointing to the dataset.
    """

    if filename.endswith(".zarr") or filename.endswith(".zip"):
        assert (
            not filename.endswith(".zip") or mode == "r"
        ), "Only reading supported for zarr ZipStore"

        logger.debug("opening zarr dataset %s in %s", ds_name, filename)
        try:
            ds = zarr.open(filename, mode=mode)[ds_name]
        except Exception as e:
            logger.error("failed to open %s/%s" % (filename, ds_name))
            raise e

        voxel_size, offset = _read_voxel_size_offset(ds, ds.order)
        shape = Coordinate(ds.shape[-len(voxel_size) :])
        roi = Roi(offset, voxel_size * shape)

        chunk_shape = ds.chunks

        logger.debug("opened zarr dataset %s in %s", ds_name, filename)
        return Array(ds, roi, voxel_size, chunk_shape=chunk_shape)

    elif filename.endswith(".n5"):
        logger.debug("opening N5 dataset %s in %s", ds_name, filename)
        ds = zarr.open(filename, mode=mode)[ds_name]

        voxel_size, offset = _read_voxel_size_offset(ds, "F")
        shape = Coordinate(ds.shape[-len(voxel_size) :])
        roi = Roi(offset, voxel_size * shape)

        chunk_shape = ds.chunks

        logger.debug("opened N5 dataset %s in %s", ds_name, filename)
        return Array(ds, roi, voxel_size, chunk_shape=chunk_shape)

    elif filename.endswith(".h5") or filename.endswith(".hdf"):
        logger.debug("opening H5 dataset %s in %s", ds_name, filename)
        ds = h5py.File(filename, mode=mode)[ds_name]

        voxel_size, offset = _read_voxel_size_offset(ds, "C")
        shape = Coordinate(ds.shape[-len(voxel_size) :])
        roi = Roi(offset, voxel_size * shape)

        chunk_shape = ds.chunks

        logger.debug("opened H5 dataset %s in %s", ds_name, filename)
        return Array(ds, roi, voxel_size, chunk_shape=chunk_shape)

    elif filename.endswith(".json"):
        logger.debug("found JSON container spec")
        with open(filename, "r") as f:
            spec = json.load(f)

        array = open_ds(spec["container"], ds_name, mode)
        return Array(
            array.data,
            Roi(spec["offset"], spec["size"]),
            array.voxel_size,
            array.roi.begin,
            chunk_shape=array.chunk_shape,
        )

    else:
        logger.error("don't know data format of %s in %s", ds_name, filename)
        raise RuntimeError("Unknown file format for %s" % filename)


def prepare_ds(
    filename: str,
    ds_name: str,
    total_roi: Roi,
    voxel_size: Coordinate,
    dtype,
    write_roi: Roi = None,
    write_size: Coordinate = None,
    num_channels: Optional[int] = None,
    compressor: Union[str, dict] = "default",
    delete: bool = False,
    force_exact_write_size: bool = False,
) -> Array:
    """Prepare a Zarr or N5 dataset.

    Args:

        filename:

            The name of the container "file" (which is actually a directory).

        ds_name:

            The name of the dataset to prepare.

        total_roi:

            The ROI of the dataset to prepare in world units.

        voxel_size:

            The size of one voxel in the dataset in world units.

        write_size:

            The size of anticipated writes to the dataset, in world units. The
            chunk size of the dataset will be set such that ``write_size`` is a
            multiple of it. This allows concurrent writes to the dataset if the
            writes are aligned with ``write_size``.

        num_channels:

            The number of channels.

        compressor:

            The compressor to use. See `zarr.get_codec` for available options.
            Defaults to gzip level 5.

        delete:

            Whether to delete an existing dataset if it was found to be
            incompatible with the other requirements. The default is not to
            delete the dataset and raise an exception instead.

        force_exact_write_size:

            Whether to use `write_size` as-is, or to first process it with
            `get_chunk_size`.

    Returns:

        A :class:`Array` pointing to the newly created dataset.
    """

    voxel_size = Coordinate(voxel_size)
    if write_size is not None:
        write_size = Coordinate(write_size)

    assert total_roi.shape.is_multiple_of(
        voxel_size
    ), "The provided ROI shape is not a multiple of voxel_size"
    assert total_roi.begin.is_multiple_of(
        voxel_size
    ), "The provided ROI offset is not a multiple of voxel_size"

    if write_roi is not None:
        logger.warning("write_roi is deprecated, please use write_size instead")

        if write_size is None:
            write_size = write_roi.shape

    if write_size is not None:
        assert write_size.is_multiple_of(
            voxel_size
        ), f"The provided write size ({write_size}) is not a multiple of voxel_size ({voxel_size})"

    if compressor == "default":
        compressor = {"id": "gzip", "level": 5}

    ds_name = ds_name.lstrip("/")

    if filename.endswith(".h5") or filename.endswith(".hdf"):
        raise RuntimeError("prepare_ds does not support HDF5 files")
    elif filename.endswith(".zarr"):
        file_format = "zarr"
    elif filename.endswith(".n5"):
        file_format = "n5"
    else:
        raise RuntimeError("Unknown file format for %s" % filename)

    if write_size is not None:
        if not force_exact_write_size:
            chunk_shape = get_chunk_shape(write_size / voxel_size)
        else:
            chunk_shape = write_size / voxel_size
    else:
        chunk_shape = None

    shape = total_roi.shape / voxel_size

    if num_channels is not None:
        shape = (num_channels,) + shape

        if chunk_shape is not None:
            chunk_shape = Coordinate((num_channels,) + chunk_shape)
        voxel_size_with_channels = Coordinate((1,) + voxel_size)

    if not os.path.isdir(filename):
        logger.debug("Creating new %s", filename)
        os.makedirs(filename)

        zarr.open(filename, mode="w")

    if not os.path.isdir(os.path.join(filename, ds_name)):
        logger.debug(
            "Creating new %s in %s with chunk_size %s and write_size %s",
            ds_name,
            filename,
            chunk_shape,
            write_size,
        )

        if compressor is not None:
            compressor = zarr.get_codec(compressor)

        root = zarr.open(filename, mode="a")
        ds = root.create_dataset(
            ds_name, shape=shape, chunks=chunk_shape, dtype=dtype, compressor=compressor
        )

        if file_format == "zarr":
            ds.attrs["resolution"] = voxel_size
            ds.attrs["offset"] = total_roi.begin
        else:
            ds.attrs["resolution"] = voxel_size[::-1]
            ds.attrs["offset"] = total_roi.begin[::-1]

        if chunk_shape is not None:
            if num_channels is not None:
                chunk_shape = chunk_shape / voxel_size_with_channels
            else:
                chunk_shape = chunk_shape / voxel_size
        return Array(ds, total_roi, voxel_size, chunk_shape=chunk_shape)

    else:
        logger.debug("Trying to reuse existing dataset %s in %s...", ds_name, filename)
        ds = open_ds(filename, ds_name, mode="a")

        compatible = True

        if ds.shape != shape:
            logger.info("Shapes differ: %s vs %s", ds.shape, shape)
            compatible = False

        if ds.roi != total_roi:
            logger.info("ROIs differ: %s vs %s", ds.roi, total_roi)
            compatible = False

        if ds.voxel_size != voxel_size:
            logger.info("Voxel sizes differ: %s vs %s", ds.voxel_size, voxel_size)
            compatible = False

        if write_size is not None and ds.data.chunks != chunk_shape:
            logger.info("Chunk shapes differ: %s vs %s", ds.data.chunks, chunk_shape)
            compatible = False

        if dtype != ds.dtype:
            logger.info("dtypes differ: %s vs %s", ds.dtype, dtype)
            compatible = False

        if not compatible:
            if not delete:
                raise RuntimeError(
                    "Existing dataset is not compatible, please manually "
                    "delete the volume at %s/%s" % (filename, ds_name)
                )

            logger.info("Existing dataset is not compatible, creating new one")

            shutil.rmtree(os.path.join(filename, ds_name))
            return prepare_ds(
                filename=filename,
                ds_name=ds_name,
                total_roi=total_roi,
                voxel_size=voxel_size,
                dtype=dtype,
                write_size=write_size,
                num_channels=num_channels,
                compressor=compressor,
            )

        else:
            logger.info("Reusing existing dataset")
            return ds


def get_chunk_shape(block_shape):
    """Get a reasonable chunk size that divides the given block size."""

    chunk_shape = Coordinate(get_chunk_size_dim(b, 256) for b in block_shape)

    logger.debug("Setting chunk size to %s", chunk_shape)

    return chunk_shape


def get_chunk_size_dim(b, target_chunk_size):
    best_k = None
    best_target_diff = 0

    for k in range(1, b + 1):
        if ((b // k) * k) % b == 0:
            diff = abs(b // k - target_chunk_size)
            if best_k is None or diff < best_target_diff:
                best_target_diff = diff
                best_k = k

    return b // best_k
