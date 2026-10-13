

from argparse import ArgumentParser, Namespace
import json
import logging
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


def save_config_file(path: str, cfg):
    """Save JSON config."""
    try:
        p = absfile(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved configuration to {path}")
    except Exception as e:
        logger.error(f"Failed to save config to {path}: {e}")


def load_config_file(path: str) -> dict[any, any]: # type: ignore
    """Load JSON. Return an object/dict."""
    if not path:
        return {}
    try:
        p = absfile(path)
        if not p.exists():
            logger.error(f"Config file not found: {path}")
            return {}
        with p.open("r", encoding="utf-8") as f:
            raw = f.read()
            cfg = json.loads(raw)
            logger.info(f"Loaded config: {path}")
            return dict(cfg)
    except Exception as e:
        logger.error(f"Failed to load config file {path}: {e}")
        return {}


def load_and_merge_config(args: Namespace, parser: ArgumentParser, transient: tuple[str, ...] = ()):
    """
    CLI flags override config values.
    Only replace a value if the CLI used something *not equal*
    to the defaultargparse value.
    `transient` flags apply to this run only: they are taken from the CLI and never saved to the config file.
    """
    conf = load_config_file(args.config)

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


def normalize_case(txt: str) -> str:
    """
    Normalize the case of a string to Title Case while erasing any extra spaces.

    Parameters
    ----------
    txt : str
        The input string to normalize.

    Returns
    -------
    str
        The normalized string.
    
    Example
    -------
    >>> normalize_case("  hello   world  ")
    'Hello World'

    >>> normalize_case("  MIXED case SEntence  ")
    'Mixed Case Sentence'

    >>> normalize_case("  eÃ‰  ")
    'EÃ©'
    """
    # Gemini Disclosure:

    # Step 1: Normalize the Unicode string (NFC ensures composed characters)
    normalized = unicodedata.normalize('NFC', txt)

    # Step 2: Define a function to capitalize the first character and lower the rest of a word
    def capitalize_match(match):
        word = match.group(0)
        # Use .capitalize() or title logic per word with proper case mapping
        # For full unicode awareness, upper/lower the first/subsequent chars:
        if not word:
            return word
        return word[0].upper() + word[1:].lower()

    # Step 3: Match Unicode word characters (supports international alphabets)
    # \w matches alphanumeric characters plus underscores in regex with re.UNICODE/default in Python 3
    capitalized = re.sub(r'\b\w+\b', capitalize_match, normalized)

    # Step 4: Collapse runs of whitespace into a single space and trim the ends
    return re.sub(r'\s+', ' ', capitalized).strip()


def generate_uuid(base: str) -> str:
    """
    Derive a UUID (Universally Unique Identifier) as a string.

    Parameters
    ---
    base : str

    Returns
    -------
    str
        The generated UUID.
    """
    namespace = uuid.NAMESPACE_URL
    return str(uuid.uuid5(namespace, base))


def identity_key(email: str = "", student_number: str = "", full_name: str = "") -> str:
    """
    What identifies a student across submissions: the email, or when the form gave none the student number,
    or when it gave neither the name. Case and surrounding spaces are ignored.

    >>> identity_key(" Ann@Mail.UToronto.ca ", "1001231617", "Ann Lee")
    'ann@mail.utoronto.ca'
    >>> identity_key("", "1001231617", "Ann Lee")
    '1001231617'
    >>> identity_key(full_name=" Ann  Lee")
    'ann lee'
    """
    return (email.strip().lower() or student_number.strip().lower()
            or " ".join(full_name.lower().split()))


def student_uuid_for(email: str = "", student_number: str = "", full_name: str = "") -> str:
    """
    Internal per-student id, derived from `identity_key`. It is a database key, not an identity: it changes if a
    student's identifying field does (e.g. a later submission that has an email when the first had none).
    """
    return generate_uuid("student|" + identity_key(email, student_number, full_name))


def application_uuid_for(program: str, submission_ts: str, email: str = "", student_number: str = "",
                         full_name: str = "") -> str:
    """Internal per-application id: the program, the submission timestamp and the student's `identity_key`."""
    return generate_uuid(f"{program}|{submission_ts.strip()}|{identity_key(email, student_number, full_name)}")


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
