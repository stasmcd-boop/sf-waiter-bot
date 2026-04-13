# SF Academy Bot v3

Готовая версия с исправленной синхронизацией меню через sitemap.xml.

## Главное
- более user-friendly интерфейс
- академия меню
- глубокий разбор блюд
- AI-тренировка продаж
- /sync_site теперь синхронизирует меню через sitemap.xml

## Railway
1. Замени файлы в репозитории
2. Убедись, что в Variables есть:
   - TELEGRAM_BOT_TOKEN
   - SF_SITE_URL=https://shaurma-food.kz
   - OPENAI_API_KEY (необязательно)
3. Сделай redeploy
4. В боте зайди под ролью директор/администратор
5. Выполни /sync_site
6. Проверь /menu_stats
