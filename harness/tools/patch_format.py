"""Pure parsing/matching for Codex and unified patches. No filesystem or model access."""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PatchEdit:
    operation: str
    path: str
    body: tuple[str, ...] = ()
    destination: str | None = None


_UNIFIED_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _diff_path(raw: str) -> str:
    value = raw.strip()
    if "\t" in value:
        value = value.split("\t", 1)[0].strip()
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return value


def parse_unified_patch(text: str) -> tuple[PatchEdit, ...]:
    """Parse a standard unified diff into the same internal edit model.

    Hunk line numbers are validated as a syntax check, but application uses
    the same exact-context matching as Codex patches. This keeps the tool
    self-contained: no external ``patch`` executable is invoked.
    """
    lines = text.splitlines()
    edits = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("--- "):
            index += 1
            continue
        source = _diff_path(line[4:])
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise ValueError("unified diff requires --- then +++ file headers")
        target = _diff_path(lines[index][4:])
        index += 1
        body = []
        while index < len(lines) and not lines[index].startswith("--- ") \
                and not lines[index].startswith("diff --git "):
            if lines[index].startswith("@@"):
                if not _UNIFIED_HUNK.match(lines[index]):
                    raise ValueError(f"invalid unified hunk header: {lines[index]!r}")
                body.append("@@")
                index += 1
                while index < len(lines) and not lines[index].startswith("@@") \
                        and not lines[index].startswith("--- ") \
                        and not lines[index].startswith("diff --git "):
                    hunk_line = lines[index]
                    if hunk_line.startswith("\\ No newline at end of file"):
                        index += 1
                        continue
                    if hunk_line == "":
                        body.append(" ")
                    else:
                        if hunk_line[0] not in " +-":
                            raise ValueError("hunk lines require a space, + or - prefix")
                        body.append(hunk_line)
                    index += 1
            else:
                index += 1
        if source == "/dev/null":
            if not target or target == "/dev/null":
                raise ValueError("invalid add-file header")
            additions = []
            for item in body:
                if item == "@@":
                    continue
                if not item.startswith("+"):
                    raise ValueError("Add File lines must start with +")
                additions.append(item)
            edits.append(PatchEdit("Add", target, tuple(additions)))
        elif target == "/dev/null":
            if not source:
                raise ValueError("invalid delete-file header")
            edits.append(PatchEdit("Delete", source, ()))
        else:
            if not body:
                raise ValueError("unified diff file has no hunks")
            edits.append(PatchEdit(
                "Update", source, tuple(body),
                target if target != source else None,
            ))
    if not edits:
        raise ValueError("unified diff contains no file edits")
    return tuple(edits)



def parse_codex_patch(text: str) -> tuple[PatchEdit, ...]:
    lines = text.splitlines()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("expected *** Begin Patch and *** End Patch")
    edits = []; i = 1
    while i < len(lines) - 1:
        line = lines[i]; i += 1
        if not any(line.startswith(f"*** {kind} File: ") for kind in ("Add", "Delete", "Update")):
            raise ValueError(f"expected Add/Update/Delete File header, got {line!r}")
        operation, path = line[4:].split(" File: ", 1)
        if not path.strip():
            raise ValueError("empty patch path")
        destination = None
        if operation == "Update" and i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
            destination = lines[i][13:]; i += 1
            if not destination.strip():
                raise ValueError("empty move destination")
        body = []
        while i < len(lines) - 1 and not any(lines[i].startswith(f"*** {kind} File: ")
                                              for kind in ("Add", "Delete", "Update")):
            body.append(lines[i]); i += 1
        if operation == "Delete" and body:
            raise ValueError("Delete File must have no body")
        if operation == "Add" and any(not line.startswith("+") for line in body):
            raise ValueError("Add File lines must start with +")
        if operation == "Update" and not body:
            raise ValueError("Update File needs at least one hunk")
        edits.append(PatchEdit(operation, path, tuple(body), destination))
    if not edits:
        raise ValueError("patch contains no edits")
    return tuple(edits)


def update_text(original: str, body: tuple[str, ...]) -> str:
    """Exact-context matching, sequential hunks, optional anchor and EOF marker."""
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines(); cursor = 0; i = 0
    while i < len(body):
        header = body[i]; i += 1
        if header != "@@" and not header.startswith("@@ "):
            raise ValueError("Update File hunk must start with @@ or @@ anchor")
        if header.startswith("@@ "):
            anchor = header[3:]
            matches = [n for n in range(cursor, len(lines)) if lines[n] == anchor]
            if not matches:
                raise ValueError(f"anchor not found: {anchor!r}")
            cursor = matches[0] + 1
        old = []; new = []; eof = False
        while i < len(body) and not body[i].startswith("@@"):
            line = body[i]; i += 1
            if line == "*** End of File":
                eof = True
                if i < len(body):
                    raise ValueError("End of File must end the update")
                break
            if not line or line[0] not in " +-":
                raise ValueError("hunk lines require a space, + or - prefix")
            if line[0] in " -": old.append(line[1:])
            if line[0] in " +": new.append(line[1:])
        matches = [n for n in range(cursor, len(lines) - len(old) + 1)
                   if lines[n:n + len(old)] == old and (not eof or n + len(old) == len(lines))]
        if not old:
            matches = [len(lines) if eof else cursor]
        if len(matches) != 1:
            raise ValueError("patch context not found or ambiguous; add exact surrounding context")
        start = matches[0]; lines[start:start + len(old)] = new; cursor = start + len(new)
    return newline.join(lines) + (newline if lines else "")
