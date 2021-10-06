# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
This package contains functions for reading and writing Parquet
tables that are not meant to be used directly, but instead are
available as readers/writers in `astropy.table`.  See
:ref:`astropy:table_io` for more details.
"""

import os
import warnings

import numpy as np

# NOTE: Do not import anything from astropy.table here.
# https://github.com/astropy/astropy/issues/6604
from astropy.utils.exceptions import AstropyUserWarning

PARQUET_SIGNATURE = b'PAR1'

__all__ = []  # nothing is publicly scoped


def parquet_identify(origin, filepath, fileobj, *args, **kwargs):
    """Checks if input is in the Parquet format.

    Parameters
    ----------
    origin : Any
    filepath : str or None
    fileobj : `~pyarrow.NativeFile` or None
    *args, **kwargs

    Returns
    -------
    is_parquet : bool
        True if 'fileobj' is not None and is a pyarrow file, or if
        'filepath' is a string ending with '.parquet' or '.parq'.
        False otherwise.
    """
    if fileobj is not None:
        try:  # safely test if pyarrow file
            pos = fileobj.tell()  # store current stream position
        except AttributeError:
            return False

        signature = fileobj.read(4)  # read first 4 bytes
        fileobj.seek(pos)  # return to original location

        return signature == PARQUET_SIGNATURE
    elif filepath is not None:
        return filepath.endswith(('.parquet', '.parq'))
    else:
        return False


def read_table_parquet(input, columns=None, schema_only=False, filters=None):
    """
    Read a Table object from a Parquet file.

    This requires `pyarrow <https://arrow.apache.org/docs/python/>`_
    to be installed.

    The ``filters`` parameter consists of predicates that are expressed
    in disjunctive normal form (DNF), like ``[[('x', '=', 0), ...], ...]``.
    DNF allows arbitrary boolean logical combinations of single column
    predicates. The innermost tuples each describe a single column predicate.
    The list of inner predicates is interpreted as a conjunction (AND),
    forming a more selective and multiple column predicate. Finally, the most
    outer list combines these filters as a disjunction (OR).

    Predicates may also be passed as List[Tuple]. This form is interpreted
    as a single conjunction. To express OR in predicates, one must
    use the (preferred) List[List[Tuple]] notation.

    Each tuple has format: (``key``, ``op``, ``value``) and compares the
    ``key`` with the ``value``.
    The supported ``op`` are:  ``=`` or ``==``, ``!=``, ``<``, ``>``, ``<=``,
    ``>=``, ``in`` and ``not in``. If the ``op`` is ``in`` or ``not in``, the
    ``value`` must be a collection such as a ``list``, a ``set`` or a
    ``tuple``.

    Examples:

    .. code-block:: python

        ('x', '=', 0)
        ('y', 'in', ['a', 'b', 'c'])
        ('z', 'not in', {'a','b'})

    Parameters
    ----------
    input : str or path-like or file-like object
        If a string or path-like object, the filename to read the table from.
        If a file-like object, the stream to read data.
    columns : list [str], optional
        Names of astropy columns to read.
        This will automatically expand to all serialized columns if
        necessary.
    schema_only : bool, optional
        Only read the schema/metadata with table information.
    filters : list [tuple] or list [list [tuple] ] or None, optional
        Rows which do not match the filter predicate will be removed from
        scanned data.  See `pyarrow.parquet.read_table()` for details.

    Returns
    -------
    table : `~astropy.table.Table`
        Table will have zero rows and only metadata information
        if schema_only is True.
    """
    try:
        import pyarrow as pa
        from pyarrow import parquet
    except ImportError:
        raise Exception("pyarrow is required to read and write parquet files")

    if not isinstance(input, (str, os.PathLike)):
        # The 'read' attribute is the key component of a generic
        # file-like object.
        if not hasattr(input, 'read'):
            raise TypeError("pyarrow can only open path-like or file-like objects.")

    schema = parquet.read_schema(input)

    # Pyarrow stores all metadata as byte-strings, so we convert
    # to UTF-8 strings here.
    if schema.metadata is not None:
        md = {key.decode('UTF-8'): schema.metadata[key].decode('UTF-8')
              for key in schema.metadata}
    else:
        md = {}

    from astropy.table import Table, meta, serialize

    # parse metadata from table yaml
    meta_dict = {}
    if 'table_meta_yaml' in md:
        meta_yaml = md.pop('table_meta_yaml').split('\n')
        meta_hdr = meta.get_header_from_yaml(meta_yaml)
        if 'meta' in meta_hdr:
            meta_dict = meta_hdr['meta']
    else:
        meta_hdr = None

    # parse and set serialized columns
    full_table_columns = {name: name for name in schema.names}
    has_serialized_columns = False
    if '__serialized_columns__' in meta_dict:
        has_serialized_columns = True
        serialized_columns = meta_dict['__serialized_columns__']
        for scol in serialized_columns:
            for name in _get_names(serialized_columns[scol]):
                full_table_columns[name] = scol

    # columns_to_read is a list of actual serialized column names, where
    # e.g. the requested column 'time' becomes ['time.jd1', 'time.jd2']
    if columns is None:
        columns_to_read = schema.names
    else:
        columns_to_read = []
        for column in columns:
            cols = [n for n in full_table_columns if column == full_table_columns[n]]
            columns_to_read.extend(cols)

        if not columns_to_read:
            raise ValueError("No columns specified were found in the table.")

        # We need to pop any unread serialized columns out of the meta_dict.
        if has_serialized_columns:
            for scol in list(meta_dict['__serialized_columns__'].keys()):
                if scol not in columns:
                    meta_dict['__serialized_columns__'].pop(scol)

    # whether to return the whole table or a formatted empty table.
    if not schema_only:
        # Read the pyarrow table, specifying columns and filters.
        pa_table = parquet.read_table(input, columns=columns_to_read, filters=filters)
        num_rows = pa_table.num_rows
    else:
        num_rows = 0

    # Now need to convert parquet table to Astropy
    dtype = []
    for name in columns_to_read:
        # Pyarrow string and byte columns do not have native length information
        # so we must determine those here.
        if schema.field(name).type == pa.string() or schema.field(name).type == pa.binary():
            md_name = f'table::len::{name}'
            if md_name in md:
                # String/bytes length from header.
                strlen = int(md[f'table::len::{name}'])
            else:
                # Find the maximum string length.
                if schema_only:
                    # Choose an arbitrary string length since
                    # are not reading in the table.
                    strlen = 10
                    warnings.warn(f"No table::len::{name} found in metadata. "
                                  f"Guessing {{strlen}} for schema.",
                                  AstropyUserWarning)
                else:
                    strlen = max([len(row.as_py()) for row in pa_table[name]])
                    warnings.warn(f"No table::len::{name} found in metadata. "
                                  f"Using longest string ({{strlen}} characters).",
                                  AstropyUserWarning)
            if schema.field(name).type == pa.string():
                dtype.append(f'U{strlen}')
            else:
                dtype.append(f'|S{strlen}')
        else:
            # Convert the pyarrow type into a numpy dtype (which is returned
            # by the to_pandas_type() method).
            dtype.append(schema.field(name).type.to_pandas_dtype())

    # Create the empty numpy record array to store the pyarrow data.
    data = np.zeros(num_rows, dtype=list(zip(columns_to_read, dtype)))

    if not schema_only:
        # Convert each column in the pyarrow table to a numpy array
        for name in columns_to_read:
            data[name][:] = pa_table[name].to_numpy()

    table = Table(data=data, meta=meta_dict)

    if meta_hdr is not None:
        # Set description, format, unit, meta from the column
        # metadata that was serialized with the table.
        header_cols = dict((x['name'], x) for x in meta_hdr['datatype'])
        for col in table.columns.values():
            for attr in ('description', 'format', 'unit', 'meta'):
                if attr in header_cols[col.name]:
                    setattr(col, attr, header_cols[col.name][attr])

    # Convert all compound columns to astropy objects
    # (e.g. time.jd1, time.jd2 into a single time column)
    table = serialize._construct_mixins_from_columns(table)

    return table


def write_table_parquet(table, output, overwrite=False):
    """
    Write a Table object to a Parquet file

    This requires `pyarrow <https://arrow.apache.org/docs/python/>`_
    to be installed.

    Parameters
    ----------
    table : `~astropy.table.Table`
        Data table that is to be written to file.
    output : str
        The filename to write the table to.
    overwrite : bool
        Whether to overwrite any existing file without warning.
    """

    from astropy.table import meta, serialize
    from astropy.utils.data_info import serialize_context_as
    try:
        import pyarrow as pa
        from pyarrow import parquet
    except ImportError:
        raise Exception("pyarrow is required to read and write Parquet files")

    if not isinstance(output, str):
        raise TypeError(f'`output` should be a string, not {output}')

    # Convert all compound columns into serialized column names, where
    # e.g. 'time' becomes ['time.jd1', 'time.jd2'].
    with serialize_context_as('parquet'):
        encode_table = serialize.represent_mixins_as_columns(table)
    # We store the encoded serialization metadata as a yaml string.
    meta_yaml = meta.get_yaml_from_table(encode_table)
    meta_yaml_str = '\n'.join(meta_yaml)

    metadata = {}
    for name, col in encode_table.columns.items():
        # Special-case string types to record the length
        if col.dtype.type is np.str_:
            metadata[f'table::len::{name}'] = str(col.dtype.itemsize//4)
        elif col.dtype.type is np.bytes_:
            metadata[f'table::len::{name}'] = str(col.dtype.itemsize)

        metadata['table_meta_yaml'] = meta_yaml_str

    # Pyarrow stores all metadata as byte-strings, so we explicitly convert
    # all metadata from UTF-8 to byte strings here.
    metadata_encode = {k.encode('UTF-8'): v.encode('UTF-8') for k, v in metadata.items()}

    # Build the pyarrow schema by converting from the numpy dtype of each
    # column to an equivalent pyarrow type with from_numpy_dtype()
    type_list = [(name, pa.from_numpy_dtype(encode_table.dtype[name].type))
                 for name in encode_table.dtype.names]
    schema = pa.schema(type_list, metadata=metadata_encode)

    if os.path.exists(output):
        if overwrite:
            # We must remove the file prior to writing below.
            os.remove(output)
        else:
            raise OSError(f"File exists: {output}")

    # We use version='2.0' for full support of datatypes including uint32.
    with parquet.ParquetWriter(output, schema, version='2.0') as writer:
        # Convert each Table column to a pyarrow array
        arrays = [pa.array(col) for col in encode_table.itercols()]
        # Create a pyarrow table from the list of arrays and the schema
        pa_table = pa.Table.from_arrays(arrays, schema=schema)
        # Write the pyarrow table to a file
        writer.write_table(pa_table)


def _get_names(_dict):
    """Recursively find the names in a serialized column dictionary.

    Parameters
    ----------
    _dict : `dict`
        Dictionary from astropy __serialized_columns__

    Returns
    -------
    all_names : `list` [`str`]
        All the column names mentioned in _dict and sub-dicts.
    """
    all_names = []
    for key in _dict:
        if isinstance(_dict[key], dict):
            all_names.extend(_get_names(_dict[key]))
        elif key == 'name':
            all_names.append(_dict['name'])
    return all_names


def register_parquet():
    """
    Register Parquet with Unified I/O.
    """
    from astropy.io import registry as io_registry
    from astropy.table import Table

    io_registry.register_reader('parquet', Table, read_table_parquet)
    io_registry.register_writer('parquet', Table, write_table_parquet)
    io_registry.register_identifier('parquet', Table, parquet_identify)
