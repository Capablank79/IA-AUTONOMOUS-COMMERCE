"""Configuración y factoría de conexiones para PostgreSQL (P.3 — Database Migrations).

Proporciona:
1. DatabaseConfig: DTO inmutable para almacenar la configuración de base de datos
   con sanitización estricta de credenciales (__repr__, DSN sanitizado, logging).
2. DatabaseConnectionFactory: Factoría de conexiones psycopg (v3) que valida
   parámetros, previene fuga de secretos y maneja conexiones de forma segura.
"""

from dataclasses import dataclass
import os
import re
from typing import Any, Dict, Mapping, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit, urlunsplit

import psycopg
from psycopg import conninfo


class DatabaseConfigError(ValueError):
    """Error de configuración de base de datos."""


class DatabaseConnectionError(RuntimeError):
    """Error al establecer o verificar la conexión con la base de datos."""


def sanitize_dsn(dsn_or_url: str) -> str:
    """Sanitiza cualquier DSN o URL de conexión reemplazando la contraseña por ***."""
    if not dsn_or_url:
        return ""
    # Redactar password en URI con regex universal
    sanitized = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", dsn_or_url)
    # Si contiene formato libpq key=value
    sanitized = re.sub(r"(password=)[^\s]+", r"\1***", sanitized, flags=re.IGNORECASE)
    return sanitized


def sanitize_error_message(msg: str) -> str:
    """Sanitiza mensajes de error eliminando passwords y tokens expuestos."""
    if not msg:
        return ""
    # Redactar password en URI
    sanitized = sanitize_dsn(msg)
    # Redactar password en texto plano como password 'xxx' o password "xxx" o password=xxx
    sanitized = re.sub(r"(password\s*['\":=]\s*['\"]?)[^'\"\s]+(['\"]?)", r"\1***\2", sanitized, flags=re.IGNORECASE)
    return sanitized


@dataclass(frozen=True)
class DatabaseConfig:
    """Configuración inmutable de conexión a PostgreSQL.
    
    Nunca expone la contraseña en logs o representaciones de cadena.
    """
    host: str
    port: int
    database: str
    user: str
    password: str
    connect_timeout: int = 10
    sslmode: Optional[str] = None
    application_name: str = "ai-autonomous-commerce"

    def __post_init__(self) -> None:
        if not self.host or not self.host.strip():
            raise DatabaseConfigError("POSTGRES_HOST cannot be empty.")
        if not (1 <= self.port <= 65535):
            raise DatabaseConfigError(f"Invalid POSTGRES_PORT '{self.port}'. Must be between 1 and 65535.")
        if not self.database or not self.database.strip():
            raise DatabaseConfigError("POSTGRES_DB cannot be empty.")
        if not self.user or not self.user.strip():
            raise DatabaseConfigError("POSTGRES_USER cannot be empty.")
        if not self.password:
            raise DatabaseConfigError("POSTGRES_PASSWORD cannot be empty.")

    def __repr__(self) -> str:
        return (
            f"DatabaseConfig(host='{self.host}', port={self.port}, "
            f"database='{self.database}', user='{self.user}', password='***', "
            f"connect_timeout={self.connect_timeout}, sslmode={self.sslmode!r})"
        )

    def __str__(self) -> str:
        return self.sanitized_dsn

    @property
    def sanitized_dsn(self) -> str:
        """DSN seguro para visualización y logs (contraseña redactada)."""
        return f"postgresql://{quote_plus(self.user)}:***@{self.host}:{self.port}/{self.database}"

    @property
    def sqlalchemy_url(self) -> str:
        """URL segura de conexión para SQLAlchemy usando psycopg3 (psycopg)."""
        return f"postgresql+psycopg://{quote_plus(self.user)}:{quote_plus(self.password)}@{self.host}:{self.port}/{self.database}"

    def build_conninfo(self) -> str:
        """Construye el string conninfo seguro para ser usado exclusivamente por el driver."""
        kwargs: Dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "password": self.password,
            "connect_timeout": self.connect_timeout,
            "application_name": self.application_name,
        }
        if self.sslmode:
            kwargs["sslmode"] = self.sslmode
        return conninfo.make_conninfo(**kwargs)

    def with_database(self, new_database_name: str) -> "DatabaseConfig":
        """Crea una copia inmutable cambiando únicamente el nombre de la base de datos de destino.
        
        Garantiza que host, port, user, password, sslmode y timeouts se preserven con total exactitud.
        """
        if not new_database_name or not new_database_name.strip():
            raise DatabaseConfigError("Target database name cannot be empty.")
        if new_database_name == self.database:
            raise DatabaseConfigError("Target database cannot be identical to current database.")
        
        return DatabaseConfig(
            host=self.host,
            port=self.port,
            database=new_database_name.strip(),
            user=self.user,
            password=self.password,
            connect_timeout=self.connect_timeout,
            sslmode=self.sslmode,
            application_name=self.application_name,
        )

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "DatabaseConfig":
        """Carga DatabaseConfig desde un diccionario o entorno del sistema."""
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        env_map = env if env is not None else os.environ

        # Si DATABASE_URL está definida, se analiza
        database_url = env_map.get("DATABASE_URL", "").strip()
        if database_url:
            try:
                parts = urlsplit(database_url)
                host = parts.hostname or "localhost"
                port = parts.port or 5432
                database = parts.path.lstrip("/") or "ia_autonomous_commerce"
                user = unquote(parts.username) if parts.username is not None else "iac_app"
                raw_password = unquote(parts.password) if parts.password is not None else env_map.get("POSTGRES_PASSWORD", "")
                
                # Parse query params (e.g. sslmode, application_name, connect_timeout)
                query_params = parse_qs(parts.query)
                sslmode = query_params.get("sslmode", [None])[0]
                app_name = query_params.get("application_name", ["ai-autonomous-commerce"])[0]
                timeout_str = query_params.get("connect_timeout", ["10"])[0]
                try:
                    connect_timeout = int(timeout_str)
                except ValueError:
                    connect_timeout = 10

                return cls(
                    host=host,
                    port=port,
                    database=database,
                    user=user,
                    password=raw_password,
                    connect_timeout=connect_timeout,
                    sslmode=sslmode,
                    application_name=app_name,
                )
            except Exception as exc:
                raise DatabaseConfigError(f"Invalid DATABASE_URL format: {exc}") from exc

        host = env_map.get("POSTGRES_HOST", "localhost").strip()
        port_raw = env_map.get("POSTGRES_PORT", "5432").strip()
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise DatabaseConfigError(f"Invalid POSTGRES_PORT '{port_raw}': must be an integer.") from exc

        database = env_map.get("POSTGRES_DB", "ia_autonomous_commerce").strip()
        user = env_map.get("POSTGRES_USER", "iac_app").strip()
        password = env_map.get("POSTGRES_PASSWORD", "")

        return cls(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
        )


class DatabaseConnectionFactory:
    """Factoría segura de conexiones PostgreSQL."""

    def __init__(self, config: DatabaseConfig) -> None:
        self._config = config

    @property
    def config(self) -> DatabaseConfig:
        return self._config

    def create_connection(self, *, autocommit: bool = False) -> psycopg.Connection:
        """Crea y retorna una nueva conexión real a PostgreSQL.
        
        Si falla, captura el error y lo sanitiza para no filtrar credenciales.
        """
        try:
            conn = psycopg.connect(
                self._config.build_conninfo(),
                autocommit=autocommit,
            )
            return conn
        except Exception as exc:
            sanitized = sanitize_error_message(str(exc))
            raise DatabaseConnectionError(
                f"Failed to connect to PostgreSQL at {self._config.sanitized_dsn}: {sanitized}"
            ) from None

    def check_connection(self) -> Dict[str, Any]:
        """Ejecuta una verificación de conexión read-only segura (SELECT current_database(), current_user, version())."""
        conn = None
        try:
            conn = self.create_connection(autocommit=True)
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), current_user, version();")
                row = cur.fetchone()
                cur.execute("SELECT 1;")
                r_one = cur.fetchone()
                
            if not row or not r_one or r_one[0] != 1:
                raise DatabaseConnectionError("Validation query failed.")
                
            return {
                "status": "ok",
                "host": self._config.host,
                "port": self._config.port,
                "database": row[0],
                "user": row[1],
                "version": row[2],
                "select_1": "OK" if r_one[0] == 1 else "FAILED",
                "database_match": row[0] == self._config.database,
                "user_match": row[1] == self._config.user,
            }
        except DatabaseConnectionError:
            raise
        except Exception as exc:
            sanitized = sanitize_error_message(str(exc))
            raise DatabaseConnectionError(f"Database check failed: {sanitized}") from None
        finally:
            if conn and not conn.closed:
                conn.close()
