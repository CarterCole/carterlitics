"""Storage: one DynamoDB table for small things, one S3 bucket for blobs.

Each has an in-memory twin with the same methods, which the tests and the local dev
server use, so nothing outside this file knows which one it has.

The table is a plain pk/sk table with TTL on `expires`. Values are strings, integers
and string sets; anything richer is stored as a JSON string by the caller.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor


class ItemTooLarge(Exception):
    pass


# ---------------------------------------------------------------- table


class MemoryTable:
    def __init__(self):
        self.items: dict[str, dict[str, dict]] = {}
        self.lock = threading.Lock()

    def _item(self, pk: str, sk: str, create: bool = True) -> dict | None:
        part = self.items.setdefault(pk, {}) if create else self.items.get(pk, {})
        if sk not in part and create:
            part[sk] = {"pk": pk, "sk": sk}
        return part.get(sk)

    def add(self, pk: str, sk: str, n: int, expires: int) -> None:
        with self.lock:
            item = self._item(pk, sk)
            item["n"] = item.get("n", 0) + n
            item["expires"] = expires

    def add_many(self, counts: dict[tuple[str, str], int], expires: int) -> None:
        for (pk, sk), n in counts.items():
            self.add(pk, sk, n, expires)

    def add_to_set(self, pk: str, sk: str, attr: str, values: set[str], expires: int) -> None:
        with self.lock:
            item = self._item(pk, sk)
            item.setdefault(attr, set()).update(values)
            item["expires"] = expires

    def put(self, item: dict) -> None:
        with self.lock:
            self.items.setdefault(item["pk"], {})[item["sk"]] = dict(item)

    def put_many(self, items: list[dict]) -> None:
        for item in items:
            self.put(item)

    def put_new(self, item: dict) -> bool:
        with self.lock:
            if item["sk"] in self.items.get(item["pk"], {}):
                return False
            self.items.setdefault(item["pk"], {})[item["sk"]] = dict(item)
            return True

    def update(self, pk: str, sk: str, add: dict | None = None, set_: dict | None = None,
               set_if_absent: dict | None = None, must_exist: bool = False) -> bool:
        with self.lock:
            if must_exist and self._item(pk, sk, create=False) is None:
                return False
            item = self._item(pk, sk)
            for k, v in (add or {}).items():
                item[k] = item.get(k, 0) + v
            for k, v in (set_ or {}).items():
                item[k] = v
            for k, v in (set_if_absent or {}).items():
                item.setdefault(k, v)
            return True

    def update_if_recent(self, pk: str, sk: str, field: str, since: int, add: dict | None = None,
                         set_: dict | None = None) -> dict | None:
        """Update the item only if it exists and its `field` is at least `since`. The item
        as it was before, or None (and no change) when there is no such item."""
        with self.lock:
            item = self._item(pk, sk, create=False)
            if item is None or item.get(field, 0) < since:
                return None
            before = _copy(item)
            for k, v in (add or {}).items():
                item[k] = item.get(k, 0) + v
            item.update(set_ or {})
            return before

    def put_unless_recent(self, item: dict, field: str, since: int) -> bool:
        """Put the item unless one is there whose `field` is at least `since`."""
        with self.lock:
            there = self.items.get(item["pk"], {}).get(item["sk"])
            if there is not None and there.get(field, 0) >= since:
                return False
            self.items.setdefault(item["pk"], {})[item["sk"]] = dict(item)
            return True

    def get(self, pk: str, sk: str) -> dict | None:
        with self.lock:
            item = self.items.get(pk, {}).get(sk)
            return _copy(item) if item else None

    def get_many(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
        with self.lock:
            return {(pk, sk): _copy(self.items[pk][sk]) for pk, sk in keys if sk in self.items.get(pk, {})}

    def query(self, pk: str, newest_first: bool = False, limit: int | None = None) -> list[dict]:
        with self.lock:
            part = self.items.get(pk, {})
            rows = [_copy(part[k]) for k in sorted(part, reverse=newest_first)]
        return rows[:limit] if limit else rows


def _copy(item: dict) -> dict:
    return {k: set(v) if isinstance(v, set) else v for k, v in item.items()}


def _to_ddb(value):
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if isinstance(value, (set, frozenset)):
        return {"SS": sorted(value)}
    if isinstance(value, (dict, list)):
        return {"S": json.dumps(value, separators=(",", ":"))}
    return {"S": str(value)}


def _from_ddb(value: dict):
    if "N" in value:
        n = value["N"]
        return int(n) if n.lstrip("-").isdigit() else float(n)
    if "SS" in value:
        return set(value["SS"])
    if "BOOL" in value:
        return value["BOOL"]
    return value.get("S", "")


class DynamoTable:
    def __init__(self, name: str):
        import boto3  # the Lambda runtime ships it; tests and local dev never get here
        from botocore.config import Config

        self.client = boto3.client("dynamodb", config=Config(max_pool_connections=32,
                                                              retries={"mode": "standard"}))
        self.name = name

    @staticmethod
    def _out(item: dict) -> dict:
        return {k: _from_ddb(v) for k, v in item.items()}

    def add(self, pk: str, sk: str, n: int, expires: int) -> None:
        self.client.update_item(
            TableName=self.name, Key={"pk": {"S": pk}, "sk": {"S": sk}},
            UpdateExpression="ADD #n :n SET #e = :e",
            ExpressionAttributeNames={"#n": "n", "#e": "expires"},
            ExpressionAttributeValues={":n": {"N": str(n)}, ":e": {"N": str(expires)}},
        )

    def add_many(self, counts: dict[tuple[str, str], int], expires: int) -> None:
        if not counts:
            return
        with ThreadPoolExecutor(max_workers=min(16, len(counts))) as pool:
            list(pool.map(lambda kv: self.add(kv[0][0], kv[0][1], kv[1], expires), counts.items()))

    def add_to_set(self, pk: str, sk: str, attr: str, values: set[str], expires: int) -> None:
        if not values:
            return
        from botocore.exceptions import ClientError

        try:
            self.client.update_item(
                TableName=self.name, Key={"pk": {"S": pk}, "sk": {"S": sk}},
                UpdateExpression="ADD #a :v SET #e = :e",
                ExpressionAttributeNames={"#a": attr, "#e": "expires"},
                ExpressionAttributeValues={":v": {"SS": sorted(values)}, ":e": {"N": str(expires)}},
            )
        except ClientError as e:
            # A day's set of visitor hashes past DynamoDB's 400 KB item limit.
            if e.response.get("Error", {}).get("Code") == "ValidationException" and "size" in str(e).lower():
                raise ItemTooLarge(str(e)) from None
            raise

    def put(self, item: dict) -> None:
        self.client.put_item(TableName=self.name, Item={k: _to_ddb(v) for k, v in item.items()})

    def put_many(self, items: list[dict]) -> None:
        for i in range(0, len(items), 25):
            requests = [{"PutRequest": {"Item": {k: _to_ddb(v) for k, v in it.items()}}}
                        for it in items[i:i + 25]]
            pending = {self.name: requests}
            for _ in range(8):
                out = self.client.batch_write_item(RequestItems=pending)
                pending = out.get("UnprocessedItems") or {}
                if not pending:
                    break

    def put_new(self, item: dict) -> bool:
        try:
            self.client.put_item(TableName=self.name, Item={k: _to_ddb(v) for k, v in item.items()},
                                 ConditionExpression="attribute_not_exists(pk)")
            return True
        except self.client.exceptions.ConditionalCheckFailedException:
            return False

    def _update_args(self, pk: str, sk: str, add: dict | None, set_: dict | None,
                     set_if_absent: dict | None = None) -> dict:
        names, values, adds, sets = {}, {}, [], []
        for i, (k, v) in enumerate((add or {}).items()):
            names[f"#a{i}"], values[f":a{i}"] = k, _to_ddb(v)
            adds.append(f"#a{i} :a{i}")
        for i, (k, v) in enumerate((set_ or {}).items()):
            names[f"#s{i}"], values[f":s{i}"] = k, _to_ddb(v)
            sets.append(f"#s{i} = :s{i}")
        for i, (k, v) in enumerate((set_if_absent or {}).items()):
            names[f"#f{i}"], values[f":f{i}"] = k, _to_ddb(v)
            sets.append(f"#f{i} = if_not_exists(#f{i}, :f{i})")
        expr = " ".join(filter(None, [f"SET {', '.join(sets)}" if sets else "",
                                      f"ADD {', '.join(adds)}" if adds else ""]))
        return {"TableName": self.name, "Key": {"pk": {"S": pk}, "sk": {"S": sk}},
                "UpdateExpression": expr, "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": values}

    def update(self, pk: str, sk: str, add: dict | None = None, set_: dict | None = None,
               set_if_absent: dict | None = None, must_exist: bool = False) -> bool:
        args = self._update_args(pk, sk, add, set_, set_if_absent)
        if must_exist:
            args["ConditionExpression"] = "attribute_exists(pk)"
        try:
            self.client.update_item(**args)
            return True
        except self.client.exceptions.ConditionalCheckFailedException:
            return False

    def update_if_recent(self, pk: str, sk: str, field: str, since: int, add: dict | None = None,
                         set_: dict | None = None) -> dict | None:
        args = self._update_args(pk, sk, add, set_)
        args["ExpressionAttributeNames"]["#since"] = field
        args["ExpressionAttributeValues"][":since"] = {"N": str(since)}
        args["ConditionExpression"] = "attribute_exists(pk) AND #since >= :since"
        args["ReturnValues"] = "ALL_OLD"
        try:
            out = self.client.update_item(**args)
        except self.client.exceptions.ConditionalCheckFailedException:
            return None
        return self._out(out.get("Attributes") or {})

    def put_unless_recent(self, item: dict, field: str, since: int) -> bool:
        try:
            self.client.put_item(TableName=self.name, Item={k: _to_ddb(v) for k, v in item.items()},
                                 ConditionExpression="attribute_not_exists(pk) OR #since < :since",
                                 ExpressionAttributeNames={"#since": field},
                                 ExpressionAttributeValues={":since": {"N": str(since)}})
            return True
        except self.client.exceptions.ConditionalCheckFailedException:
            return False

    def get(self, pk: str, sk: str) -> dict | None:
        out = self.client.get_item(TableName=self.name, Key={"pk": {"S": pk}, "sk": {"S": sk}})
        return self._out(out["Item"]) if "Item" in out else None

    def get_many(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
        """BatchGetItem, 100 keys a call, retrying what DynamoDB leaves unprocessed."""
        out: dict[tuple[str, str], dict] = {}
        unique = list(dict.fromkeys(keys))
        for i in range(0, len(unique), 100):
            pending = {self.name: {"Keys": [{"pk": {"S": pk}, "sk": {"S": sk}} for pk, sk in unique[i:i + 100]]}}
            for _ in range(5):
                res = self.client.batch_get_item(RequestItems=pending)
                for item in res.get("Responses", {}).get(self.name, []):
                    row = self._out(item)
                    out[(row["pk"], row["sk"])] = row
                pending = res.get("UnprocessedKeys") or {}
                if not pending:
                    break
        return out

    def query(self, pk: str, newest_first: bool = False, limit: int | None = None) -> list[dict]:
        rows, start = [], None
        while True:
            args = {"TableName": self.name, "KeyConditionExpression": "pk = :pk",
                    "ExpressionAttributeValues": {":pk": {"S": pk}}, "ScanIndexForward": not newest_first}
            if limit:
                args["Limit"] = limit - len(rows)
            if start:
                args["ExclusiveStartKey"] = start
            result = self.client.query(**args)
            rows += [self._out(i) for i in result.get("Items", [])]
            start = result.get("LastEvaluatedKey")
            if not start or (limit and len(rows) >= limit):
                return rows


# ---------------------------------------------------------------- blobs


class MemoryBlobs:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.lock = threading.Lock()

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        with self.lock:
            self.objects[key] = data

    def keys(self, prefix: str) -> list[str]:
        with self.lock:
            return sorted(k for k in self.objects if k.startswith(prefix))

    def get(self, key: str) -> bytes:
        with self.lock:
            return self.objects[key]


class S3Blobs:
    def __init__(self, bucket: str):
        import boto3

        self.client = boto3.client("s3")
        self.bucket = bucket

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def keys(self, prefix: str) -> list[str]:
        pages = self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix)
        return sorted(o["Key"] for page in pages for o in page.get("Contents", []))

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
