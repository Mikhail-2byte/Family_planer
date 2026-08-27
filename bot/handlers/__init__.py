from bot.handlers import admin, new_entry, remind, views

# Порядок важен: views и remind идут раньше мастера, иначе команда, набранная
# посреди `/new`, будет проглочена как текст записи
routers = [admin.router, views.router, remind.router, new_entry.router]
