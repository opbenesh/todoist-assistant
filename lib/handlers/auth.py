from telegram.ext import filters

from lib.config import TELEGRAM_USER_ID

WHITELIST_FILTER = filters.User(user_id=TELEGRAM_USER_ID)
