from bot.handlers import admin, capture, new_entry, remind, views
from bot.middlewares import drop_wizard_state

# Порядок важен: views и remind идут раньше мастера, иначе команда, набранная
# посреди `/new`, будет проглочена как текст записи.
# capture — наоборот, последним: обращением к боту считается и ответ на его
# сообщение, так что раньше мастера он перехватывал бы реплай на «Что записать?»
routers = [
    admin.router,
    views.router,
    remind.router,
    new_entry.router,
    capture.router,
]

# Всем, кроме самого мастера: перехваченное сообщение обрывает `/new` явно,
# а не оставляет состояние висеть до следующей реплики
for _router in (admin.router, views.router, remind.router):
    _router.message.middleware(drop_wizard_state)
