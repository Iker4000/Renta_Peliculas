"""Servicio de rentas.

No posee el catalogo: lo consume. Cada renta es una composicion de dos
invocaciones al servicio de catalogo (leer para obtener el ETag, escribir
enviandolo en If-Match) con reintento cuando otro cliente se adelanta.
"""

import os
import threading
import itertools
import time
import random

import uvicorn
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field

from comun import (PUERTOS, url_de, permitir_origenes, exigir_clave, Metricas,
                   ClienteServicio, CLAVE_API, registrar_servicio)

MAXIMO_REINTENTOS = 5

# Ventana entre la lectura del ETag y la escritura. En un sistema real la
# ocupan validaciones y latencia de red; aqui se hace configurable para poder
# medir como crece la probabilidad de conflicto con su duracion.
VENTANA_MEDIA = float(os.environ.get("VENTANA_SEGUNDOS", "0"))

app = FastAPI(
    title="Servicio de Rentas",
    description=(
        "Gestiona las rentas componiendo llamadas al servicio de catalogo. "
        "Resuelve los conflictos de concurrencia optimista reintentando."
    ),
    version="1.0.0",
)
permitir_origenes(app)

metricas = Metricas("rentas")
metricas.reintentos = 0
catalogo = ClienteServicio("catalogo")

_candado = threading.Lock()
_rentas: dict[int, dict] = {}
_siguiente_id = itertools.count(1)


class SolicitudRenta(BaseModel):
    cliente: int = Field(..., description="Identificador del cliente", examples=[3])
    titulo: str = Field(..., description="Titulo a rentar", examples=["The Matrix"])


def _cabeceras(etag: str | None = None) -> dict:
    cabeceras = {"X-API-Key": CLAVE_API}
    if etag:
        cabeceras["If-Match"] = etag
    return cabeceras


@app.post("/rentas", status_code=201, tags=["rentas"],
          dependencies=[Depends(exigir_clave)])
def crear_renta(solicitud: SolicitudRenta):
    """Renta una pelicula coordinandose con el servicio de catalogo.

    El ciclo leer-modificar-escribir se repite mientras el catalogo responda
    412, que significa que el recurso cambio entre la lectura y la escritura.
    """
    metricas.peticiones += 1
    reintentos = 0

    for intento in range(1, MAXIMO_REINTENTOS + 1):
        # 1. Lectura: obtener el estado actual y su ETag
        lectura = catalogo.get(f"/catalogo/{solicitud.titulo}")
        if lectura.status_code == 404:
            raise HTTPException(status_code=404,
                                detail=f"'{solicitud.titulo}' no existe en el catalogo")
        etag = lectura.headers.get("ETag")

        if not lectura.json().get("disponible"):
            raise HTTPException(
                status_code=409,
                detail=f"'{solicitud.titulo}' no esta disponible")

        if VENTANA_MEDIA:
            time.sleep(random.expovariate(1 / VENTANA_MEDIA))

        # 2. Escritura condicionada al ETag leido
        escritura = catalogo.post(
            f"/catalogo/{solicitud.titulo}/rentar",
            json={"cliente": solicitud.cliente},
            headers=_cabeceras(etag))

        if escritura.status_code == 200:
            with _candado:
                identificador = next(_siguiente_id)
                _rentas[identificador] = {
                    "id": identificador,
                    "cliente": solicitud.cliente,
                    "titulo": solicitud.titulo,
                    "desde": time.time(),
                    "reintentos": reintentos,
                }
                registro = _rentas[identificador]
            return {"mensaje": "Renta registrada", "renta": registro,
                    "intentos": intento}

        if escritura.status_code in (409, 412):
            # Otro cliente modifico el recurso: se vuelve a leer y reintentar
            reintentos += 1
            metricas.conflictos += 1
            metricas.reintentos += 1
            continue

        metricas.errores += 1
        raise HTTPException(status_code=escritura.status_code,
                            detail=escritura.json().get("detail", "Error en el catalogo"))

    raise HTTPException(
        status_code=503,
        detail=f"No se pudo rentar '{solicitud.titulo}' tras {MAXIMO_REINTENTOS} intentos")


@app.get("/rentas", tags=["rentas"])
def listar_rentas():
    """Rentas activas conocidas por este servicio."""
    metricas.peticiones += 1
    with _candado:
        return {"rentas": list(_rentas.values())}


@app.delete("/rentas/{identificador}", tags=["rentas"],
            dependencies=[Depends(exigir_clave)])
def cerrar_renta(identificador: int):
    """Devuelve la pelicula al catalogo y cierra la renta."""
    metricas.peticiones += 1
    with _candado:
        renta = _rentas.get(identificador)
    if renta is None:
        raise HTTPException(status_code=404, detail=f"No existe la renta {identificador}")

    for _ in range(MAXIMO_REINTENTOS):
        lectura = catalogo.get(f"/catalogo/{renta['titulo']}")
        etag = lectura.headers.get("ETag")
        escritura = catalogo.post(
            f"/catalogo/{renta['titulo']}/devolver",
            json={"cliente": renta["cliente"]},
            headers=_cabeceras(etag))

        if escritura.status_code == 200:
            with _candado:
                _rentas.pop(identificador, None)
            return {"mensaje": f"Renta {identificador} cerrada",
                    "titulo": renta["titulo"]}

        if escritura.status_code in (409, 412):
            metricas.conflictos += 1
            continue

        metricas.errores += 1
        raise HTTPException(status_code=escritura.status_code,
                            detail=escritura.json().get("detail", "Error en el catalogo"))

    raise HTTPException(status_code=503, detail="No se pudo cerrar la renta")


# --- Capa transversal de gestion --------------------------------------------

@app.get("/health", tags=["gestion"])
def salud():
    """Comprueba tambien la dependencia: un servicio no esta sano si su
    dependencia no responde."""
    try:
        dependencia = catalogo.get("/health").status_code == 200
    except Exception:
        dependencia = False
    return {"estado": "vivo" if dependencia else "degradado",
            "servicio": "rentas",
            "dependencia_catalogo": dependencia}


@app.get("/metricas", tags=["gestion"])
def ver_metricas():
    datos = metricas.como_dict()
    with _candado:
        datos["rentas_activas"] = len(_rentas)
    datos["reintentos_totales"] = metricas.reintentos
    return datos


@app.on_event("startup")
def al_arrancar():
    registrar_servicio("rentas", url_de("rentas"))


if __name__ == "__main__":
    print("=== SERVICIO DE RENTAS ===")
    print(f"OpenAPI: http://127.0.0.1:{PUERTOS['rentas']}/docs\n")
    uvicorn.run(app, host="0.0.0.0", port=PUERTOS["rentas"], log_level="warning")
