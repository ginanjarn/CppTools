"""sublime settings helper"""

from contextlib import contextmanager
import sublime

from .constant import SETTINGS_BASENAME

@contextmanager
def Settings(
    *, base_name: str = SETTINGS_BASENAME, save: bool = False
) -> sublime.Settings:
    """sublime settings"""

    yield sublime.load_settings(base_name)
    if save:
        sublime.save_settings(base_name)
