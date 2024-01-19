"""Geospatial transforms"""

from contextlib import ExitStack
from functools import partial
import math
import numpy as np
import warnings

from affine import Affine

from rasterio.env import env_ctx_if_needed
from rasterio._transform import (
    _transform_from_gcps,
    RPCTransformerBase,
    GCPTransformerBase,
)
from rasterio.enums import TransformDirection, TransformMethod
from rasterio.control import GroundControlPoint
from rasterio.rpc import RPC
from rasterio.errors import TransformError, RasterioDeprecationWarning

IDENTITY = Affine.identity()
GDAL_IDENTITY = IDENTITY.to_gdal()


class TransformMethodsMixin:
    """Mixin providing methods for calculations related
    to transforming between rows and columns of the raster
    array and the coordinates.

    These methods are wrappers for the functionality in
    `rasterio.transform` module.

    A subclass with this mixin MUST provide a `transform`
    property.

    """

    def xy(
        self,
        row,
        col,
        z=None,
        offset="center",
        transform_method=TransformMethod.affine,
        **rpc_options
    ):
        """Get the coordinates x, y of a pixel at row, col.

        The pixel's center is returned by default, but a corner can be returned
        by setting `offset` to one of `ul, ur, ll, lr`.

        Parameters
        ----------
        row : int
            Pixel row.
        col : int
            Pixel column.
        z : float, optional
            Height associated with coordinates. Primarily used for RPC based
            coordinate transformations. Ignored for affine based
            transformations. Default: 0.
        offset : str, optional
            Determines if the returned coordinates are for the center of the
            pixel or for a corner.
        transform_method: TransformMethod, optional
            The coordinate transformation method. Default: `TransformMethod.affine`.
        rpc_options: dict, optional
            Additional arguments passed to GDALCreateRPCTransformer

        Returns
        -------
        tuple
            x, y

        """
        transform = getattr(self, transform_method.value)
        if transform_method is TransformMethod.gcps:
            transform = transform[0]
        if not transform:
            raise AttributeError("Dataset has no {}".format(transform_method))
        return xy(transform, row, col, zs=z, offset=offset, **rpc_options)

    def index(
        self,
        x,
        y,
        z=None,
        op=math.floor,
        precision=None,
        transform_method=TransformMethod.affine,
        **rpc_options
    ):
        """Get the (row, col) index of the pixel containing (x, y).

        Parameters
        ----------
        x : float
            x value in coordinate reference system
        y : float
            y value in coordinate reference system
        z : float, optional
            Height associated with coordinates. Primarily used for RPC based
            coordinate transformations. Ignored for affine based
            transformations. Default: 0.
        op : function, optional (default: math.floor)
            Function to convert fractional pixels to whole numbers (floor,
            ceiling, round)
        transform_method: TransformMethod, optional
            The coordinate transformation method. Default: `TransformMethod.affine`.
        rpc_options: dict, optional
            Additional arguments passed to GDALCreateRPCTransformer
        precision : int, optional
            This parameter is unused, deprecated in rasterio 1.3.0, and
            will be removed in version 2.0.0.

        Returns
        -------
        tuple
            (row index, col index)

        """
        if precision is not None:
            warnings.warn(
                "The precision parameter is unused, deprecated, and will be removed in 2.0.0.",
                RasterioDeprecationWarning,
            )

        transform = getattr(self, transform_method.value)
        if transform_method is TransformMethod.gcps:
            transform = transform[0]
        if not transform:
            raise AttributeError("Dataset has no {}".format(transform_method))
        return rowcol(transform, x, y, zs=z, op=op, **rpc_options)


def get_transformer(transform, **rpc_options):
    """Return the appropriate transformer class"""
    if transform is None:
        raise ValueError("Invalid transform")
    if isinstance(transform, Affine):
        transformer_cls = partial(AffineTransformer, transform)
    elif isinstance(transform, RPC):
        transformer_cls = partial(RPCTransformer, transform, **rpc_options)
    else:
        transformer_cls = partial(GCPTransformer, transform)
    return transformer_cls


def tastes_like_gdal(seq):
    """Return True if `seq` matches the GDAL geotransform pattern."""
    return tuple(seq) == GDAL_IDENTITY or (
        seq[2] == seq[4] == 0.0 and seq[1] > 0 and seq[5] < 0)


def guard_transform(transform):
    """Return an Affine transformation instance."""
    if not isinstance(transform, Affine):
        if tastes_like_gdal(transform):
            raise TypeError(
                "GDAL-style transforms have been deprecated.  This "
                "exception will be raised for a period of time to highlight "
                "potentially confusing errors, but will eventually be removed.")
        else:
            transform = Affine(*transform)
    return transform


def from_origin(west, north, xsize, ysize):
    """Return an Affine transformation given upper left and pixel sizes.

    Return an Affine transformation for a georeferenced raster given
    the coordinates of its upper left corner `west`, `north` and pixel
    sizes `xsize`, `ysize`.

    """
    return Affine.translation(west, north) * Affine.scale(xsize, -ysize)


def from_bounds(west, south, east, north, width, height):
    """Return an Affine transformation given bounds, width and height.

    Return an Affine transformation for a georeferenced raster given
    its bounds `west`, `south`, `east`, `north` and its `width` and
    `height` in number of pixels.

    """
    return Affine.translation(west, north) * Affine.scale(
        (east - west) / width, (south - north) / height)


def array_bounds(height, width, transform):
    """Return the bounds of an array given height, width, and a transform.

    Return the `west, south, east, north` bounds of an array given
    its height, width, and an affine transform.

    """
    a, b, c, d, e, f, _, _, _ = transform
    if b == d == 0:
        west, south, east, north = c, f + e * height, c + a * width, f
    else:
        c0x, c0y = c, f
        c1x, c1y = transform * (0, height)
        c2x, c2y = transform * (width, height)
        c3x, c3y = transform * (width, 0)
        xs = (c0x, c1x, c2x, c3x)
        ys = (c0y, c1y, c2y, c3y)
        west, south, east, north = min(xs), min(ys), max(xs), max(ys)

    return west, south, east, north


def xy(transform, rows, cols, zs=None, offset='center', **rpc_options):
    """Get the x and y coordinates of pixels at `rows` and `cols`.

    The pixel's center is returned by default, but a corner can be returned
    by setting `offset` to one of `ul, ur, ll, lr`.

    Supports affine, Ground Control Point (GCP), or Rational Polynomial
    Coefficients (RPC) based coordinate transformations.

    Parameters
    ----------
    transform : Affine or sequence of GroundControlPoint or RPC
        Transform suitable for input to AffineTransformer, GCPTransformer, or RPCTransformer.
    rows : list or int
        Pixel rows.
    cols : int or sequence of ints
        Pixel columns.
    zs : list or float, optional
        Height associated with coordinates. Primarily used for RPC based
        coordinate transformations. Ignored for affine based
        transformations. Default: 0.
    offset : str, optional
        Determines if the returned coordinates are for the center of the
        pixel or for a corner.
    rpc_options : dict, optional
        Additional arguments passed to GDALCreateRPCTransformer.

    Returns
    -------
    xs : float or list of floats
        x coordinates in coordinate reference system
    ys : float or list of floats
        y coordinates in coordinate reference system

    """
    transformer_cls = get_transformer(transform, **rpc_options)
    with transformer_cls() as transformer:
        return transformer.xy(rows, cols, zs=zs, offset=offset)


def rowcol(transform, xs, ys, zs=None, op=math.floor, precision=None, **rpc_options):
    """Get rows and cols of the pixels containing (x, y).

    Parameters
    ----------
    transform : Affine or sequence of GroundControlPoint or RPC
        Transform suitable for input to AffineTransformer, GCPTransformer, or RPCTransformer.
    xs : list or float
        x values in coordinate reference system.
    ys : list or float
        y values in coordinate reference system.
    zs : list or float, optional
        Height associated with coordinates. Primarily used for RPC based
        coordinate transformations. Ignored for affine based
        transformations. Default: 0.
    op : function
        Function to convert fractional pixels to whole numbers (floor, ceiling,
        round).
    precision : int or float, optional
        This parameter is unused, deprecated in rasterio 1.3.0, and
        will be removed in version 2.0.0.
    rpc_options : dict, optional
        Additional arguments passed to GDALCreateRPCTransformer.

    Returns
    -------
    rows : list of ints
        list of row indices
    cols : list of ints
        list of column indices

    """
    if precision is not None:
        warnings.warn(
            "The precision parameter is unused, deprecated, and will be removed in 2.0.0.",
            RasterioDeprecationWarning,
        )

    transformer_cls = get_transformer(transform, **rpc_options)
    with transformer_cls() as transformer:
        return transformer.rowcol(xs, ys, zs=zs, op=op)


def from_gcps(gcps):
    """Make an Affine transform from ground control points.

    Parameters
    ----------
    gcps : sequence of GroundControlPoint
        Such as the first item of a dataset's `gcps` property.

    Returns
    -------
    Affine

    """
    return Affine.from_gdal(*_transform_from_gcps(gcps))


class TransformerBase:
    """Generic GDAL transformer base class

    Notes
    -----
    Subclasses must have a _transformer attribute and implement a `_transform` method.

    """
    def __init__(self):
        self._transformer = None

    @staticmethod
    def _ensure_arr_input(xs, ys, zs=None):
        """Ensure all input coordinates are mapped to array-like objects

        Raises
        ------
        TransformError
            If input coordinates are not all of the same length
        """
        xs = np.atleast_1d(xs)
        try:
            if zs is None:
                b = np.broadcast(xs, ys)
            else:
                b = np.broadcast(xs, ys, zs)
        except ValueError as err:
            raise TransformError(str(err))

        if b.ndim != 1:
            raise TransformError("Invalid dimensions after broadcast")

        pts = np.zeros((3, b.size))
        pts[0] = xs
        pts[1] = ys
        if zs is not None:
            pts[2] = zs
        return pts[0], pts[1], pts[2]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def rowcol(self, xs, ys, zs=None, op=math.floor, precision=None):
        """Get rows and cols coordinates given geographic coordinates.

        Parameters
        ----------
        xs, ys : float or list of float
            Geographic coordinates
        zs : float or list of float, optional
            Height associated with coordinates. Primarily used for RPC based
            coordinate transformations. Ignored for affine based
            transformations. Default: 0.
        op : function, optional (default: math.floor)
            Function to convert fractional pixels to whole numbers (floor,
            ceiling, round)
        precision : int, optional (default: None)
            This parameter is unused, deprecated in rasterio 1.3.0, and
            will be removed in version 2.0.0.

        Raises
        ------
        ValueError
            If input coordinates are not all equal length

        Returns
        -------
        tuple of float or list of float.

        """
        if precision is not None:
            warnings.warn(
                "The precision parameter is unused, deprecated, and will be removed in 2.0.0.",
                RasterioDeprecationWarning,
            )

        AS_ARR = any((hasattr(xs, "__iter__"), hasattr(ys, "__iter__"), hasattr(zs, "__iter__")))
        xs, ys, zs = self._ensure_arr_input(xs, ys, zs=zs)

        try:
            new_cols, new_rows = self._transform(
                xs, ys, zs, transform_direction=TransformDirection.reverse
            )
        except TypeError:
            raise TransformError("Invalid inputs")

        if AS_ARR:
            if isinstance(op, np.ufunc):
                op(new_cols, out=new_cols)
                op(new_rows, out=new_rows)
                return new_rows.tolist(), new_cols.tolist()
            else:
                new_cols = map(op, new_cols.tolist())
                new_rows = map(op, new_rows.tolist())
                return list(new_rows), list(new_cols)
        else:
            return op(new_rows[0]), op(new_cols[0])

    def xy(self, rows, cols, zs=None, offset='center'):
        """
        Returns geographic coordinates given dataset rows and cols coordinates

        Parameters
        ----------
        rows, cols : int or list of int
            Image pixel coordinates
        zs : float or list of float, optional
            Height associated with coordinates. Primarily used for RPC based
            coordinate transformations. Ignored for affine based
            transformations. Default: 0.
        offset : str, optional
            Determines if the returned coordinates are for the center of the
            pixel or for a corner. Available options include center, ul, ur, ll,
            lr.
        Raises
        ------
        ValueError
            If input coordinates are not all equal length

        Returns
        -------
        tuple of float or list of float

        """
        AS_ARR = True if hasattr(rows, "__iter__") else False
        rows, cols, zs = self._ensure_arr_input(rows, cols, zs=zs)

        if offset == 'center':
            coff, roff = (0.5, 0.5)
        elif offset == 'ul':
            coff, roff = (0, 0)
        elif offset == 'ur':
            coff, roff = (1, 0)
        elif offset == 'll':
            coff, roff = (0, 1)
        elif offset == 'lr':
            coff, roff = (1, 1)
        else:
            raise TransformError("Invalid offset")

        try:
            # shift input coordinates according to offset
            T = IDENTITY.translation(coff, roff)
            identity_transformer = AffineTransformer(T)
            offset_cols, offset_rows = identity_transformer._transform(
                cols, rows, zs, transform_direction=TransformDirection.forward
            )
            new_xs, new_ys = self._transform(
                offset_cols, offset_rows, zs, transform_direction=TransformDirection.forward
            )
            if len(new_xs) == 1 and not AS_ARR:
                return (new_xs[0], new_ys[0])
            else:
                return (list(new_xs), list(new_ys))
        except TypeError:
            raise TransformError("Invalid inputs")

    def _transform(self, xs, ys, zs, transform_direction):
        raise NotImplementedError


class GDALTransformerBase(TransformerBase):
    def __init__(self):
        super().__init__()
        self._env = ExitStack()

    def close(self):
        pass

    def __enter__(self):
        self._env.enter_context(env_ctx_if_needed())
        return self

    def __exit__(self, *args):
        self.close()
        self._env.close()


class AffineTransformer(TransformerBase):
    """A pure Python class related to affine based coordinate transformations."""
    def __init__(self, affine_transform):
        super().__init__()
        if not isinstance(affine_transform, Affine):
            raise ValueError("Not an affine transform")
        self._transformer = affine_transform
        self._transform_arr = np.empty((3, 3))

    def _transform(self, xs, ys, zs, transform_direction):
        input_matrix = np.empty((3, len(xs)))
        input_matrix[0] = xs
        input_matrix[1] = ys
        input_matrix[2] = 1

        if transform_direction is TransformDirection.forward:
            transform = self._transformer
        else:
            transform = ~self._transformer
        arr = self._transform_arr
        arr.flat[:] = transform
        
        output_matrix = np.matmul(arr, input_matrix, out=input_matrix)
        return output_matrix[0], output_matrix[1]

    def __repr__(self):
        return "<AffineTransformer>"


class RPCTransformer(RPCTransformerBase, GDALTransformerBase):
    """
    Class related to Rational Polynomial Coeffecients (RPCs) based
    coordinate transformations.

    Uses GDALCreateRPCTransformer and GDALRPCTransform for computations. Options
    for GDALCreateRPCTransformer may be passed using `rpc_options`.
    Ensure that GDAL transformer objects are destroyed by calling `close()`
    method or using context manager interface.

    """
    def __init__(self, rpcs, **rpc_options):
        if not isinstance(rpcs, (RPC, dict)):
            raise ValueError("RPCTransformer requires RPC")
        super().__init__(rpcs, **rpc_options)

    def __repr__(self):
        return "<{} RPCTransformer>".format(
            self.closed and 'closed' or 'open')


class GCPTransformer(GCPTransformerBase, GDALTransformerBase):
    """
    Class related to Ground Control Point (GCPs) based
    coordinate transformations.

    Uses GDALCreateGCPTransformer and GDALGCPTransform for computations.
    Ensure that GDAL transformer objects are destroyed by calling `close()`
    method or using context manager interface. If `tps` is set to True,
    uses GDALCreateTPSTransformer and GDALTPSTransform instead.

    """
    def __init__(self, gcps, tps=False):
        if len(gcps) and not isinstance(gcps[0], GroundControlPoint):
            raise ValueError("GCPTransformer requires sequence of GroundControlPoint")
        super().__init__(gcps, tps)

    def __repr__(self):
        return "<{} GCPTransformer>".format(
            self.closed and 'closed' or 'open')
