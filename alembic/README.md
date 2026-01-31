# Alembic migrations

Используется для безопасного обновления схемы БД.

## Применить миграции (Windows cmd)

```bat
.venv\Scripts\activate
set DB_PATH=data/botfit.sqlite3
alembic upgrade head
```

## Создать новую миграцию (ручной шаблон)

```bat
alembic revision -m "add something"
```

Дальше правишь файл в `alembic/versions/`.

