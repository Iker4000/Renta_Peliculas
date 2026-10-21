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

CLAVE_API = os.environ.get("CLAVE_API", "practica6")


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


def verificar_if_match(if_match: str | None, etag_actual: str):
    """Precondiciones de HTTP: 428 si falta, 412 si no coincide."""
    if if_match is None:
        raise HTTPException(
            status_code=428,
            detail="Se requiere la cabecera If-Match con el ETag leido",
        )
    if if_match != etag_actual and if_match != "*":
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
