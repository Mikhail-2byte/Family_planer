from bot.handlers import admin, new_entry, views

# Порядок важен: views идёт раньше мастера, иначе команда, набранная посреди
# `/new`, будет проглочена как текст записи
routers = [admin.router, views.router, new_entry.router]
