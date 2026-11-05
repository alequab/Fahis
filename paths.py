"""Where Fahis keeps what it writes. The installed app can't write beside its own program files."""
import os
from pathlib import Path

DATA_HOME = Path(os.environ.get("FAHIS_DATA_DIR") or Path(__file__).parent)
