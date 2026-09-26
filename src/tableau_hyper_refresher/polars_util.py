from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, List, Optional, cast

import polars as pl

logger = logging.getLogger(__name__)


@dataclass
class ColStats:
    nullable: bool = True
    # type ladder: bool < int < float < decimal < timestamp < text
    kind: str = "bool"
    max_len: int = 0
    dec_precision: int = 0
    dec_scale: int = 0


TYPE_ORDER = ["bool", "int", "float", "decimal", "timestamp", "text"]


def promote(a: str, b: str) -> str:
    return TYPE_ORDER[max(TYPE_ORDER.index(a), TYPE_ORDER.index(b))]


def is_bool(s: str) -> bool:
    return s.lower() in {"true", "false", "t", "f", "0", "1", "yes", "no", "y", "n"}


def parse_decimal(s: str) -> Optional[tuple[int, int]]:
    # returns (precision, scale)
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    tup = d.as_tuple()
    digits = len(tup.digits)
    exponent = tup.exponent if isinstance(tup.exponent, int) else 0
    scale = -exponent if exponent < 0 else 0
    precision = max(digits, scale)  # conservative
    return precision, scale


def infer_value_kind(s: str) -> str:
    if s == "":
        return "bool"  # "no signal"
    if is_bool(s):
        return "bool"
    # int?
    try:
        int(s)
        return "int"
    except ValueError:
        pass
    # decimal BEFORE float so it can ever win
    if parse_decimal(s) is not None:
        return "decimal"
    # float?
    try:
        float(s)
        return "float"
    except ValueError:
        pass
    # timestamp: (recommend real parsing instead of heuristic)
    if any(ch in s for ch in ["-", ":", "T", "Z", "/"]) and len(s) >= 8:
        return "timestamp"
    return "text"


def ddl_type(stats: ColStats, dialect: str = "postgres") -> str:
    # minimal, safe defaults
    if stats.kind == "bool":
        return "BOOLEAN"
    if stats.kind == "int":
        return "BIGINT"
    if stats.kind == "float":
        return "DOUBLE PRECISION"
    if stats.kind == "decimal":
        p = max(stats.dec_precision, 1)
        s = max(stats.dec_scale, 0)
        return f"NUMERIC({p},{s})"
    if stats.kind == "timestamp":
        return "TIMESTAMP"
    # text
    # if you want VARCHAR with observed max length:
    # return f"VARCHAR({max(stats.max_len,1)})"
    return "TEXT"


def infer_schema_from_csv_parts(
    files: List[str],
    separator: str = ",",
    batch_size: int = 50_000,
) -> tuple[List[str], List[ColStats]]:
    """
    Infer the schema of a CSV file from a list of files.
    There are libraries that can do this better, but this is a simple implementation that works for our needs.
    """
    if not files:
        raise FileNotFoundError("No files provided")

    # Read just header from first file
    hdr = pl.read_csv(files[0], n_rows=0, separator=separator).columns
    stats = [ColStats() for _ in hdr]

    for f in files:
        # stream in batches; only a batch is in memory
        for df in pl.read_csv(
            f,
            separator=separator,
            has_header=True,
            batch_size=batch_size,
            ignore_errors=True,  # helpful if occasional bad rows
            infer_schema_length=0,  # don't try to infer upfront
            schema={c: pl.Utf8 for c in hdr},  # read as strings
        ).iter_slices():
            # ensure column order and names
            df = df.select(hdr)

            for i, c in enumerate(hdr):
                s = df[c]

                # nulls / empties
                non_null = s.drop_nulls()
                # Treat empty string as null for inference
                non_empty = non_null.filter(non_null.str.len_chars() > 0)

                if non_empty.len() == 0:
                    continue  # no signal in this batch

                stats[i].nullable = True  # keep nullable unless you want strictness
                # update max string length
                maxlen = int(cast(Any, non_empty.str.len_chars().max()))
                if maxlen > stats[i].max_len:
                    stats[i].max_len = maxlen

                # infer kind by sampling values in this batch (still bounded)
                # If you want “most accurate”, iterate all values; usually sampling is enough.
                vals = non_empty.head(5_000).to_list()
                batch_kind = "bool"
                for v in vals:
                    k = infer_value_kind(v)
                    batch_kind = promote(batch_kind, k)
                    if batch_kind == "text":
                        break

                    if k == "decimal":
                        ps = parse_decimal(v)
                        if ps:
                            p, sc = ps
                            stats[i].dec_precision = max(stats[i].dec_precision, p)
                            stats[i].dec_scale = max(stats[i].dec_scale, sc)

                stats[i].kind = promote(stats[i].kind, batch_kind)

    return hdr, stats


if __name__ == "__main__":
    cols, st = infer_schema_from_csv_parts([])
