from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from app.models.entities import AuditLog, DailySale, User


def mysql_table_ddl(model: type[User] | type[DailySale] | type[AuditLog]) -> str:
    return str(CreateTable(model.__table__).compile(dialect=mysql.dialect()))


def test_mysql_schema_uses_unsigned_ids_and_financially_safe_types() -> None:
    user_ddl = mysql_table_ddl(User)
    sale_ddl = mysql_table_ddl(DailySale)
    audit_ddl = mysql_table_ddl(AuditLog)

    assert "BIGINT UNSIGNED NOT NULL AUTO_INCREMENT" in user_ddl
    assert "TINYINT UNSIGNED" in user_ddl
    assert "phone REGEXP '^05[0-9]{8}$'" in user_ddl
    assert "INTEGER UNSIGNED NOT NULL" in sale_ddl
    assert "NUMERIC(14, 2) NOT NULL" in sale_ddl
    assert "reason VARCHAR(500)" in audit_ddl
