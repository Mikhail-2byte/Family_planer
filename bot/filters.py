from aiogram import F

# Бот живёт только в общем семейном чате — в личке семьи нет
IN_GROUP = F.chat.type.in_({"group", "supergroup"})

# То же для колбэков: у CallbackQuery чат лежит внутри сообщения
IN_GROUP_CB = F.message.chat.type.in_({"group", "supergroup"})
