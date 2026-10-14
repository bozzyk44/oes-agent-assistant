"""Поправка оценки токенов по имени модели.

Оценка входа до вызова сверяется с фактическим входом из ответа провайдера; если оценка
оказалась меньше, множитель для этой модели растёт и переживает перезапуск. Вниз он
не идёт: таблица хранит наибольший. Это не данные пользователя и не набор: построчной
защиты нет, строка одна на модель.
"""

from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade() -> None:
    op.execute(
        """
        create table app.token_calibration (
            model text primary key,
            factor double precision not null,
            updated_at timestamptz not null default now(),
            constraint token_calibration_factor check (factor >= 1)
        )
        """
    )


def downgrade() -> None:
    op.execute("drop table app.token_calibration")
