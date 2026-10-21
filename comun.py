"""Piezas compartidas por los servicios.

Reune lo que atraviesa todas las capas de la pila: la configuracion por
variables de entorno (capa de comunicaciones), el calculo de ETags (capa
de mensajes), el cliente para invocar a otros servicios (capa de procesos)
y las utilidades de seguridad y gestion.
"""

import os
import time
import hashlib
import json

import httpx
from fastapi import Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# --- Capa de comunicaciones: donde vive cada servicio -----------------------
# En local son puertos distintos; en el despliegue, URLs completas que se
# inyectan por variable de entorno (principio de la aplicacion de 12 factores).

PUERTOS = {
    "registro": 8000,
    "catalogo": 8001,
    "rentas": 8002,
    "agregador": 8003,
}


def url_de(servicio: str) -> str:
    """Resuelve en que direccion escucha un servicio.

    En local cada uno usa un puerto distinto de 127.0.0.1. En el despliegue
    la direccion llega por variable de entorno. Se tolera que venga sin
    esquema (Render entrega "host:puerto" en algunas propiedades) y con una
    barra final, porque httpx exige un esquema explicito.
    """
    variable = f"URL_{servicio.upper()}"
    destino = os.environ.get(variable)
    if not destino:
        return f"http://127.0.0.1:{PUERTOS[servicio]}"
    destino = destino.strip().rstrip("/")
    if "://" not in destino:
        destino = f"http://{destino}"
    return destino


# --- Capa transversal de seguridad ------------------------------------------

CLAVE_API = os.environ.get("CLAVE_API", "renta-peliculas")


def exigir_clave(x_api_key: str = Header(default=None)):
    """Dependencia de FastAPI: protege las operaciones de escritura."""
    if x_api_key != CLAVE_API:
        raise HTTPException(
            status_code=401,
            detail="Falta la cabecera X-API-Key o su valor es incorrecto",
        )
    return x_api_key


def permitir_origenes(app):
    """CORS: el frontend vive en otro dominio (GitHub Pages)."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["ETag"],   # sin esto el navegador no ve el ETag
    )


# --- Capa de mensajes: control de concurrencia optimista --------------------

def calcular_etag(estado) -> str:
    """Huella del estado actual del recurso.

    Es la version que el cliente devolvera en If-Match. Si entre su lectura
    y su escritura alguien modifico el recurso, la huella ya no coincide y
    la operacion se rechaza.
    """
    datos = json.dumps(estado, sort_keys=True, ensure_ascii=False)
    return '"' + hashlib.sha256(datos.encode("utf-8")).hexdigest()[:16] + '"'


def _etiqueta_desnuda(etiqueta: str) -> str:
    """Deja la etiqueta opaca, sin el prefijo de validador debil.

    Un proxy que recomprime la respuesta esta obligado por el RFC 9110 a
    marcar el ETag como debil, antependiendole "W/", porque la
    representacion que entrega ya no es identica byte a byte a la del
    origen. Render lo hace. El cliente devuelve entonces
    If-Match: W/"abc" contra un ETag interno "abc", y una comparacion
    literal fallaria siempre con 412: la concurrencia optimista quedaria
    inservible en la nube aunque funcione en local.

    Aqui la debilidad la introduce la infraestructura, no un cambio de
    estado del recurso, asi que se compara la etiqueta desnuda.
    """
    etiqueta = etiqueta.strip()
    if etiqueta[:2].upper() == "W/":
        etiqueta = etiqueta[2:]
    return etiqueta


def verificar_if_match(if_match: str | None, etag_actual: str):
    """Precondiciones de HTTP: 428 si falta, 412 si no coincide."""
    if if_match is None:
        raise HTTPException(
            status_code=428,
            detail="Se requiere la cabecera If-Match con el ETag leido",
        )
    if if_match.strip() == "*":
        return
    # If-Match admite una lista de etiquetas separadas por comas.
    candidatas = [_etiqueta_desnuda(t) for t in if_match.split(",")]
    if _etiqueta_desnuda(etag_actual) not in candidatas:
        raise HTTPException(
            status_code=412,
            detail=f"El recurso cambio desde su lectura (ETag actual {etag_actual})",
        )


# --- Capa de procesos: invocacion entre servicios ---------------------------

class ClienteServicio:
    """Cliente HTTP hacia otro servicio, resuelto por descubrimiento.

    Mantiene la conexion abierta entre invocaciones (keep-alive). Abrir un
    socket nuevo por peticion multiplica la latencia: es la diferencia entre
    pagar el saludo de tres vias una vez o pagarlo siempre.
    """

    def __init__(self, servicio: str, tiempo_limite: float = 10.0):
        self.servicio = servicio
        self.tiempo_limite = tiempo_limite
        self._http = httpx.Client(
            timeout=tiempo_limite,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )

    @property
    def base(self) -> str:
        return url_de(self.servicio)

    def peticion(self, metodo: str, ruta: str, **extra) -> httpx.Response:
        return self._http.request(metodo, f"{self.base}{ruta}", **extra)

    def get(self, ruta: str, **extra) -> httpx.Response:
        return self.peticion("GET", ruta, **extra)

    def post(self, ruta: str, **extra) -> httpx.Response:
        return self.peticion("POST", ruta, **extra)

    def delete(self, ruta: str, **extra) -> httpx.Response:
        return self.peticion("DELETE", ruta, **extra)


def registrar_servicio(nombre: str, url: str):
    """Publica este servicio en el registro. No es fatal si el registro no esta."""
    try:
        ClienteServicio("registro", tiempo_limite=3.0).post(
            "/servicios", json={"nombre": nombre, "url": url})
        print(f"[{nombre}] Registrado en el servicio de descubrimiento")
    except httpx.HTTPError:
        print(f"[{nombre}] El registro no responde; se continua sin descubrimiento")


INTERVALO_REGISTRO = 120.0
_ultimo_registro = {"cuando": 0.0}


def asegurar_registro(nombre: str, url: str, forzar: bool = False):
    """Se republica en el registro si hace rato del ultimo alta.

    El directorio del registro vive en memoria, asi que se vacia cuando el
    registro se reinicia o la plataforma lo suspende por inactividad. Un
    alta hecha una sola vez al arrancar no sobrevive a eso: el servicio
    sigue vivo pero ya no aparece en el directorio.

    Se resuelve republicandose de forma perezosa, aprovechando el trafico
    que el servicio ya esta atendiendo, en vez de con un latido periodico.
    Un latido mantendria despierto al registro de forma permanente y
    agotaria la cuota del plan gratuito; asi, mientras nadie use el
    sistema, no se genera trafico alguno.

    Es el mismo motivo por el que los registros reales (Eureka, Consul)
    tratan las altas como arrendamientos que hay que renovar y no como
    inscripciones permanentes.
    """
    ahora = time.time()
    if not forzar and ahora - _ultimo_registro["cuando"] < INTERVALO_REGISTRO:
        return
    _ultimo_registro["cuando"] = ahora
    registrar_servicio(nombre, url)


# --- Capa transversal de gestion --------------------------------------------

class Metricas:
    """Contadores que expone el endpoint de gestion."""

    def __init__(self, nombre: str):
        self.nombre = nombre
        self.inicio = time.time()
        self.peticiones = 0
        self.conflictos = 0
        self.errores = 0

    def como_dict(self) -> dict:
        return {
            "servicio": self.nombre,
            "activo_segundos": round(time.time() - self.inicio, 1),
            "peticiones_atendidas": self.peticiones,
            "conflictos_409_412": self.conflictos,
            "errores": self.errores,
        }
