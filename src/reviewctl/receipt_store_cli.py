"""Explicit receipt storage commands; storage observations never grant approval."""

from __future__ import annotations

import json
import os
import sys
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

from reviewctl.api import ReviewClient
from reviewctl.errors import ConfigError, Diagnostic, JournalOperationError, exit_code_for
from reviewctl.filesystem import (
    confined_directory_descriptor,
    confined_regular_descriptor,
    confined_relative_directory_descriptor,
    confined_relative_regular_descriptor,
)
from reviewctl.receipt_store import PotzalReceiptStore, ReceiptStoreError


def _output(payload: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, sort_keys=True))
        return
    diagnostic = payload.get("diagnostic")
    stream = sys.stderr if diagnostic else sys.stdout
    print(f"storage: {payload['status']}", file=stream)
    if "objectDigest" in payload:
        print(f"object: {payload['objectDigest']}", file=stream)
    if payload.get("receipt"):
        print(f"receipt: {payload['receipt']}", file=stream)
    if diagnostic:
        print(f"diagnostic: {diagnostic['code']}: {diagnostic['message']}", file=stream)
        if diagnostic["next"]:
            print(f"next: {diagnostic['next']}", file=stream)


def _read_receipt(path: Path) -> bytes:
    try:
        with confined_regular_descriptor(path, os.O_RDONLY) as descriptor:
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                return stream.read(4 * 1024 * 1024 + 1)
    except OSError as error:
        raise ReceiptStoreError(
            Diagnostic(
                "receipt_invalid",
                "could not read a regular receipt file",
                next="choose an existing canonical receipt file without symlinks",
            )
        ) from error


def _persist_download(client: ReviewClient, digest: str, contents: bytes) -> Path:
    """Write below the original project/state identities, never re-resolved paths."""
    parts = ("shared", digest.removeprefix("sha256:"))
    target = client.project_dir / ".reviewctl" / Path(*parts) / "receipt.json"
    with ExitStack() as descriptors:
        project_descriptor = descriptors.enter_context(
            confined_directory_descriptor(
                client.project_dir, expected_identity=client._project_identity
            )
        )
        state_descriptor = descriptors.enter_context(
            confined_relative_directory_descriptor(project_descriptor, (".reviewctl",))
        )
        metadata = os.fstat(state_descriptor)
        if (metadata.st_dev, metadata.st_ino) != client.journal()._parent_identity:
            raise OSError("project state directory identity changed")
        output_descriptor = descriptors.enter_context(
            confined_relative_directory_descriptor(state_descriptor, parts, create=True)
        )
        output_metadata = os.fstat(output_descriptor)
        output_identity = (output_metadata.st_dev, output_metadata.st_ino)
        os.fchmod(output_descriptor, 0o700)
        try:
            with confined_relative_regular_descriptor(
                output_descriptor, Path("receipt.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL
            ) as descriptor:
                receipt_metadata = os.fstat(descriptor)
                receipt_identity = (receipt_metadata.st_dev, receipt_metadata.st_ino)
                with os.fdopen(os.dup(descriptor), "wb") as stream:
                    stream.write(contents)
                    stream.flush()
                    os.fsync(stream.fileno())
        except FileExistsError:
            with confined_relative_regular_descriptor(
                output_descriptor, Path("receipt.json"), os.O_RDONLY
            ) as descriptor:
                receipt_metadata = os.fstat(descriptor)
                receipt_identity = (receipt_metadata.st_dev, receipt_metadata.st_ino)
                with os.fdopen(os.dup(descriptor), "rb") as stream:
                    existing = stream.read(4 * 1024 * 1024 + 1)
                if existing != contents:
                    raise ReceiptStoreError(
                        Diagnostic(
                            "receipt_invalid",
                            "download conflicts with existing local bytes",
                            next="inspect the local artifact; existing bytes were preserved",
                        )
                    ) from None
                os.fchmod(descriptor, 0o600)
        advertised_parent = descriptors.enter_context(
            confined_directory_descriptor(target.parent, expected_identity=output_identity)
        )
        advertised_receipt = descriptors.enter_context(
            confined_relative_regular_descriptor(
                advertised_parent, Path("receipt.json"), os.O_RDONLY
            )
        )
        metadata = os.fstat(advertised_receipt)
        if (metadata.st_dev, metadata.st_ino) != receipt_identity:
            raise OSError("downloaded receipt pathname identity changed")
    return target


def store_receipt(args: Any) -> int:
    payload: dict[str, Any] = {"status": "failed"}
    try:
        project = Path(args.project).expanduser().resolve()
        client = ReviewClient.from_project(project)
        if client.config.potzal is None:
            raise ConfigError("configure [evidence.potzal] before sharing receipts")
        journal = client.journal()
        violations = journal.verify()
        if violations:
            raise JournalOperationError(
                Diagnostic(
                    "journal_corrupt",
                    "; ".join(violations),
                    next="run reviewctl journal verify before retrying storage",
                )
            )
        store = PotzalReceiptStore(client.config.potzal, client.config.project.project_id)
        local_path = None
        if args.receipts_command == "push":
            local_path = Path(args.receipt).expanduser()
            stored = store.push(_read_receipt(local_path))
            event_type = "receipt_stored"
            payload["status"] = "stored"
        else:
            stored = store.pull(args.digest)
            local_path = _persist_download(client, stored.object_digest, stored.receipt_bytes)
            event_type = "receipt_retrieved"
            payload["status"] = "retrieved"
        payload.update(
            {
                "projectId": stored.project_id,
                "objectDigest": stored.object_digest,
                "receiptSha256": stored.receipt_sha256,
                "receipt": str(local_path),
            }
        )
        journal.append(
            {
                "type": event_type,
                "reviewId": "",
                "objectDigest": stored.object_digest,
                "receiptBytesSha256": stored.receipt_sha256,
                "store": "potzal",
                "namespace": stored.namespace,
                "release": stored.release,
            }
        )
    except ReceiptStoreError as error:
        diagnostic = error.diagnostic
    except JournalOperationError as error:
        diagnostic = replace(
            error.diagnostic,
            next=error.diagnostic.next
            or "run reviewctl journal verify; reconcile local journal storage, then retry",
        )
    except ConfigError as error:
        diagnostic = Diagnostic(
            "config_invalid",
            str(error),
            next="check project.id and [evidence.potzal] in reviewctl.toml",
        )
    except OSError, ValueError:
        diagnostic = Diagnostic(
            "evidence_store_failed",
            "could not persist receipt storage evidence locally",
            next="check the private .reviewctl directory and journal, then retry",
        )
    else:
        _output(payload, args.format)
        return 0
    payload["diagnostic"] = diagnostic.to_dict()
    _output(payload, args.format)
    return exit_code_for(diagnostic.code)


def add_receipt_store_commands(commands: Any) -> None:
    receipts = commands.add_parser("receipts", help="store or retrieve canonical receipt artifacts")
    operations = receipts.add_subparsers(dest="receipts_command", required=True)
    push = operations.add_parser("push", help="publish exact receipt bytes to configured Potzal")
    push.add_argument("receipt")
    pull = operations.add_parser("pull", help="retrieve and verify a project-associated artifact")
    pull.add_argument("digest")
    for operation in (push, pull):
        operation.add_argument("--project", default=".")
        operation.add_argument("--format", choices=("text", "json"), default="text")
        operation.set_defaults(handler=store_receipt)
