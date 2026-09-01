import logging
from pathlib import Path


def setup_logging(args, config):
    logger = logging.getLogger()
    if getattr(args, "debug", False):
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

    class ConsoleFilter(logging.Filter):

        def filter(self, record):
            if args.debug or not (getattr(args, "log_file", None)
                                  or config.get("defaults", {}).get("log_file")):
                return True
            msg = record.getMessage()
            if msg.startswith("AWS:"):
                return False
            if all(k in msg for k in ('frame=', 'fps=', 'size=', 'time=', 'bitrate=', 'speed=')):
                return False
            return True

    ch.addFilter(ConsoleFilter())
    if logger.hasHandlers():
        logger.handlers.clear()
    logger.addHandler(ch)
    log_file_path = (getattr(args, "log_file", None)
                     or config.get("defaults", {}).get("log_file"))
    if log_file_path:
        try:
            Path(log_file_path).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_file_path)
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(
                "%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"))
            logger.addHandler(fh)
            logging.info(f"All logs will be written to: {log_file_path}")
        except IOError as e:
            logging.error(f"Error setting up file logger to {log_file_path}: {e}. "
                          "Continuing without file logging.")
