

from argparse import ArgumentParser, Namespace
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import uuid

import unicodedata

logger = logging.getLogger(__name__)

def setup_logging(args):
    # Add extra logging level
    addLoggingLevel('TRACE', logging.DEBUG - 5)

    verbosity = args['verbosity']
    log_file = args['log_file']

    level = logging.WARNING
    if verbosity == 'INFO':
        level = logging.INFO
    elif verbosity == 'DEBUG':
        level = logging.DEBUG
    elif verbosity >= 'TRACE':
        level = logging.TRACE # type: ignore

    # Console encodings like cp1252 can't print every character in names; replace instead of raising
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    handlers = [logging.StreamHandler(sys.stdout)]
    format_string = "%(asctime)s [%(levelname)s] %(message)s"

    if log_file:
        log_path = absfile(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, mode='a', encoding='utf-8'))

    logging.basicConfig(
        level=level,
        format=format_string,
        datefmt="%H:%M:%S",
        handlers=handlers
    )


# https://stackoverflow.com/a/35804945
def addLoggingLevel(levelName, levelNum, methodName=None):
    """
    Comprehensively adds a new logging level to the `logging` module and the
    currently configured logging class.

    `levelName` becomes an attribute of the `logging` module with the value
    `levelNum`. `methodName` becomes a convenience method for both `logging`
    itself and the class returned by `logging.getLoggerClass()` (usually just
    `logging.Logger`). If `methodName` is not specified, `levelName.lower()` is
    used.

    To avoid accidental clobberings of existing attributes, this method will
    raise an `AttributeError` if the level name is already an attribute of the
    `logging` module or if the method name is already present

    Example
    -------
    >>> addLoggingLevel('TRACE', logging.DEBUG - 5)
    >>> logging.getLogger(__name__).setLevel("TRACE")
    >>> logging.getLogger(__name__).trace('that worked')
    >>> logging.trace('so did this')
    >>> logging.TRACE
    5

    """
    if not methodName:
        methodName = levelName.lower()

    if hasattr(logging, levelName):
        raise AttributeError('{} already defined in logging module'.format(levelName))
    if hasattr(logging, methodName):
        raise AttributeError('{} already defined in logging module'.format(methodName))
    if hasattr(logging.getLoggerClass(), methodName):
        raise AttributeError('{} already defined in logger class'.format(methodName))

    # This method was inspired by the answers to Stack Overflow post
    # http://stackoverflow.com/q/2183233/2988730, especially
    # http://stackoverflow.com/a/13638084/2988730
    def logForLevel(self, message, *args, **kwargs):
        if self.isEnabledFor(levelNum):
            self._log(levelNum, message, args, **kwargs)

    def logToRoot(message, *args, **kwargs):
        logging.log(levelNum, message, *args, **kwargs)

    logging.addLevelName(levelNum, levelName)
    setattr(logging, levelName, levelNum)
    setattr(logging.getLoggerClass(), methodName, logForLevel)
    setattr(logging, methodName, logToRoot)


class ConfigError(ValueError):
    """The config file exists but cannot be read as a JSON object."""


def save_config_file(path: str, cfg):
    """Save JSON config. The file is written to a temporary name and then moved into place, so a failure never leaves it half-written."""
    tmp = None
    try:
        p = absfile(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
        logger.info(f"Saved configuration to {path}")
    except Exception as e:
        logger.error(f"Failed to save config to {path}: {e}")
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def load_config_file(path: str, strict: bool = False) -> dict[any, any]: # type: ignore
    """
    Load JSON. Return an object/dict. A missing file gives {}. A file that cannot be read as a JSON object gives {}
    and an error in the log, or with `strict` raises `ConfigError`, so a caller that writes the file back never
    replaces a config it could not read.
    """
    if not path:
        return {}
    p = absfile(path)
    if not p.exists():
        logger.error(f"Config file not found: {path}")
        return {}
    try:
        cfg = json.loads(p.read_text(encoding="utf-8-sig"))
        if not isinstance(cfg, dict):
            raise ValueError(f"expected a JSON object, got {type(cfg).__name__}")
    except (OSError, ValueError) as e:
        if strict:
            raise ConfigError(f"Cannot read the config file {p}: {e}. Fix or remove it; it was left unchanged.") from e
        logger.error(f"Failed to load config file {path}: {e}")
        return {}
    logger.info(f"Loaded config: {path}")
    return cfg


def load_and_merge_config(args: Namespace, parser: ArgumentParser, transient: tuple[str, ...] = ()):
    """
    CLI flags override config values.
    Only replace a value if the CLI used something *not equal*
    to the defaultargparse value.
    `transient` flags apply to this run only: they are taken from the CLI and never saved to the config file.
    A config file that exists but cannot be read raises `ConfigError` and is left as it is (it would otherwise be
    overwritten, losing settings such as the `smtp` section).
    """
    conf = load_config_file(args.config, strict=True)

    for key, value in vars(args).items():
        # skip config-related keys
        if key in ("config", "save_config", "testing"):
            continue

        if key in transient:
            conf[key] = value
        # CLI overrides config if explicitly provided
        elif value != parser.get_default(key):
            conf[key] = value
        else:
            # fallback to config value if exists, otherwise the default
            conf[key] = conf.get(key, value)

    save_config_file(args.config, {k: v for k, v in conf.items() if k not in transient})

    return conf


# Letters that do not split into a base letter and an accent are spelled out. Anything else that is not ASCII
# is dropped: this program works in plain ASCII (accents are removed, other scripts and emoji are not kept).
_FOLD = str.maketrans({"\u00df": "ss", "\u1e9e": "SS", "\u00e6": "ae", "\u00c6": "AE", "\u0153": "oe",
                       "\u0152": "OE", "\u00f8": "o", "\u00d8": "O", "\u0142": "l", "\u0141": "L",
                       "\u0111": "d", "\u0110": "D", "\u00f0": "d", "\u00d0": "D", "\u00fe": "th",
                       "\u00de": "Th", "\u0131": "i", "\u0127": "h", "\u0126": "H"})
# typographic apostrophes and every kind of dash are written the plain way, so "O\u2019Neil" matches "O'Neil"
_PUNCTUATION = {**{ord(c): "'" for c in "\u2018\u2019\u201b\u02bc\u02b9\u00b4`\u2032"},
                **{ord(c): "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63"}}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WORD = re.compile(r"[A-Za-z0-9_]+")


def clean_text(txt) -> str:
    """
    Text made plain ASCII and comparable: accents removed (`Jos\u00e9` becomes `Jose`), look-alike forms (fullwidth
    letters, ligatures, superscript digits, non-breaking spaces) replaced by the plain ones, typographic apostrophes
    and dashes written the plain way, runs of whitespace collapsed to one space, and everything else that is not
    ASCII (other scripts, emoji, zero-width and control characters) dropped.

    >>> clean_text("\\ufeff Jos\\u00e9\\u200b\\u00a0Garc\\u00eda ")
    'Jose Garcia'
    >>> clean_text("D\\u2019Angelo-Smith \\u2013 Jr")
    "D'Angelo-Smith - Jr"
    >>> clean_text("\\uff21\\ufb01 \\u00b2 Stra\\u00dfe")
    'Afi 2 Strasse'
    >>> clean_text("\\u674e \\u660e Ann \\U0001f600\\ud800")
    'Ann'
    >>> clean_text(None)
    ''
    """
    txt = unicodedata.normalize("NFKD", str(txt if txt is not None else "").translate(_PUNCTUATION)).translate(_FOLD)
    txt = txt.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", _CONTROL.sub("", txt)).strip()


def normalize_case(txt: str) -> str:
    """
    Normalize the case of a string to Title Case while erasing any extra spaces, as plain ASCII (see `clean_text`).
    Names are compared in this form, so `JOS\u00c9 garc\u00eda` and `Jose Garcia` are the same name. Applying it twice
    changes nothing.

    >>> normalize_case("  hello   world  ")
    'Hello World'
    >>> normalize_case("  MIXED case SEntence  ")
    'Mixed Case Sentence'
    >>> normalize_case("jose\\u0301 GARC\\u00cdA") == normalize_case("JOS\\u00c9 garc\\u00eda") == "Jose Garcia"
    True
    >>> normalize_case("mary\\u2013jane o\\u2019neil\\u200b")
    "Mary-Jane O'Neil"
    """
    return _WORD.sub(lambda m: m.group(0)[0].upper() + m.group(0)[1:].lower(), clean_text(txt))


def generate_uuid(base: str, kind: str = "") -> str:
    """
    Derive a UUID (Universally Unique Identifier) as a string.

    `kind` says what the UUID identifies (e.g. "application:sap", "student:email"). Each kind has its own
    namespace, so the same text hashed as two different kinds gives two different UUIDs and kinds can never
    collide. Without a kind the UUID is the plain URL-namespace one (used for the roster rows in `uoft_data`).

    >>> generate_uuid("a@x.ca", "student:email") != generate_uuid("a@x.ca", "student:name")
    True
    >>> generate_uuid("a@x.ca") == generate_uuid("a@x.ca")
    True
    """
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"utsu:{kind}") if kind else uuid.NAMESPACE_URL
    return str(uuid.uuid5(namespace, base))


def identity_key(email: str = "", student_number: str = "", full_name: str = "") -> str:
    """
    What identifies a student across submissions: the email, or when the form gave none the student number,
    or when it gave neither the name. Case and surrounding spaces are ignored, and the text is made ASCII (see
    `clean_text`). If a value was given but has nothing left once made ASCII (a name in another script), a hash of
    what was given is used, so different students never share a key.

    >>> identity_key(" Ann@Mail.UToronto.ca ", "1001231617", "Ann Lee")
    'ann@mail.utoronto.ca'
    >>> identity_key("", "1001231617", "Ann Lee")
    '1001231617'
    >>> identity_key(full_name=" Jos\\u00e9  Garc\\u00eda")
    'jose garcia'
    >>> identity_key(full_name="\\u674e\\u660e") != identity_key(full_name="\\u738b\\u82b3") != ""
    True
    """
    key = (clean_text(email).replace(" ", "").casefold() or clean_text(student_number).casefold()
           or clean_text(full_name).casefold())
    if key:
        return key
    given = "|".join(str(v).strip() for v in (email, student_number, full_name))
    return "raw-" + hashlib.sha256(given.encode("utf-8", "replace")).hexdigest()[:16] if given.strip("|") else ""


def identity_key_type(email: str = "", student_number: str = "", full_name: str = "") -> str:
    """
    Which field `identity_key` used: "email", "student_number", "name", or "none" when there was nothing.

    >>> identity_key_type("a@x.ca", "1", "Ann"), identity_key_type("", "1", "Ann"), identity_key_type(full_name="Ann")
    ('email', 'student_number', 'name')
    >>> identity_key_type()
    'none'
    >>> identity_key_type("\\u200b ", "", "")
    'unreadable'
    """
    if clean_text(email):
        return "email"
    if clean_text(student_number):
        return "student_number"
    if clean_text(full_name):
        return "name"
    return "unreadable" if any(str(v).strip() for v in (email, student_number, full_name)) else "none"


def typed_uuid(base: str, kind: str) -> str:
    """
    A self-describing id: the UUID for `base` in the namespace of `kind`, written `<kind>:<uuid>`, for example
    `student:email:3f2a...` or `application:sap:9c1d...`. This is how every id this program stores is written, so the
    kind is tracked alongside the uuid. The kind may itself contain colons; the uuid is what follows the last one.
    In SQLite, `uuid LIKE 'student:%'` selects a kind.

    >>> typed_uuid("a@x.ca", "student:email").startswith("student:email:")
    True
    >>> typed_uuid("a@x.ca", "student:email") == typed_uuid("a@x.ca", "student:email")
    True
    >>> typed_uuid("a@x.ca", "student:email") != typed_uuid("a@x.ca", "student:name")
    True
    """
    return f"{kind}:{generate_uuid(base, kind)}"


def parse_id(text: str) -> tuple[str, str]:
    """
    Split an id into (kind, uuid). Text without a kind (for example an id from before ids carried their type)
    comes back as ("", text).

    >>> parse_id("application:sap:9c1d")
    ('application:sap', '9c1d')
    >>> parse_id(typed_uuid("x", "roster"))[0]
    'roster'
    >>> parse_id("plain-id")
    ('', 'plain-id')
    >>> parse_id("")
    ('', '')
    """
    text = (text or "").strip()
    kind, colon, value = text.rpartition(":")
    return (kind, value) if colon and kind and value else ("", text)


def id_kind(text: str) -> str:
    """The kind an id was made for ("" when it does not say)."""
    return parse_id(text)[0]


def student_uuid_for(email: str = "", student_number: str = "", full_name: str = "") -> str:
    """
    Internal per-student id, derived from `identity_key`, of kind `student:<key type>` (see `identity_key_type`),
    so an email-based id and a name-based id are visibly different kinds of id. It is a database key, not an
    identity: it changes if a student's identifying field does (e.g. a later submission that has an email when the
    first had none), which is what the kind in the id is there to show.

    >>> id_kind(student_uuid_for("a@x.ca")), id_kind(student_uuid_for(full_name="Ann Lee"))
    ('student:email', 'student:name')
    """
    key_type = identity_key_type(email, student_number, full_name)
    return typed_uuid(identity_key(email, student_number, full_name), f"student:{key_type}")


def application_uuid_for(program: str, submission_ts: str, email: str = "", student_number: str = "",
                         full_name: str = "") -> str:
    """Internal per-application id of kind `application:<program>`: the submission timestamp and the student's key."""
    return typed_uuid(f"{submission_ts.strip()}|{identity_key(email, student_number, full_name)}",
                      f"application:{program}")


def find_project_root(path: Path | str | None = None) -> Path:
    """
    Find the project root, defined as the directory that holds `pyproject.toml` (or the `src` folder).

    Searches upward from `path` (a file or directory; defaults to this file's location), so the result
    does not depend on the current working directory.
    If no root is found, the path is returned unchanged.

    >>> find_project_root(Path("/nonexistent/place")) == Path("/nonexistent/place")
    True
    """
    start = Path(path) if path is not None else Path(__file__)
    resolved = start.resolve()
    current = resolved if resolved.is_dir() else resolved.parent

    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
        if candidate.name == "src":
            return candidate.parent
        if (candidate / "src").is_dir():
            return candidate

    return start

def absfile(relative_path: Path | str) -> Path:
    """
    Resolve a path relative to the project root into an absolute path.

    Absolute paths are returned unchanged.

    >>> absfile("src/schema/uoft_data.sql") == find_project_root() / "src" / "schema" / "uoft_data.sql"
    True
    """
    path = Path(relative_path)
    if path.is_absolute():
        return path
    return find_project_root() / path

if __name__ == '__main__':
    import doctest
    doctest.testmod()
