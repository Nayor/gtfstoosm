"""Run the MobilityData GTFS validator when it is installed.

The validator (https://github.com/MobilityData/gtfs-validator) is a Java program. The
full Docker image carries it (see the Dockerfile); elsewhere it is found through
$GTFS_VALIDATOR_JAR, or at the default place below. Without Java or without the jar the
feed is simply not checked.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

JAR_ENV = "GTFS_VALIDATOR_JAR"
DEFAULT_JAR = "/opt/gtfs-validator/gtfs-validator-cli.jar"


def find_validator() -> tuple[str, str] | None:
    """(java, jar) if the validator can be run, else None."""
    java = shutil.which("java")
    jar = os.environ.get(JAR_ENV) or DEFAULT_JAR
    if java and Path(jar).is_file():
        return java, jar
    return None


def run_validator(feed: str, out_dir: Path) -> Path:
    """Validate a feed; returns the path of the report.json written in out_dir."""
    found = find_validator()
    if found is None:
        raise RuntimeError("The GTFS validator is not installed")
    java, jar = found

    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Checking the feed with the GTFS validator")
    result = subprocess.run(
        [java, "-jar", jar, "-i", str(feed), "-o", str(out_dir)],
        capture_output=True,
        text=True,
    )
    report = out_dir / "report.json"
    if result.returncode != 0 or not report.exists():
        tail = (result.stderr or result.stdout).strip().splitlines()[-5:]
        raise RuntimeError(
            f"The GTFS validator failed (exit {result.returncode}): " + " / ".join(tail)
        )
    return report
