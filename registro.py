"""Servicio de descubrimiento (capa de Procesos).

Equivale al registro de nombres de la practica 5, pero expuesto como
servicio web: los demas servicios se publican aqui y los consumidores
los resuelven por nombre en lugar de conocer sus direcciones.
"""

import time
import threading

import uvicorn
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel

from comun import PUERTOS, permitir_origenes, exigir_clave, Metricas

app = FastAPI(
    title="Servicio de Descubrimiento",
    description="Directorio de servicios: traduce un nombre en una URL.",
    version="1.0.0",
)
permitir_origenes(app)

metricas = Metricas("registro")
_candado = threading.Lock()
_servicios: dict[str, dict] = {}


class Registro(BaseModel):
    nombre: str
    url: str


@app.post("/servicios", status_code=201, tags=["descubrimiento"])
def publicar(entrada: Registro):
    """Publica un servicio en el directorio."""
    metricas.peticiones += 1
    with _candado:
        _servicios[entrada.nombre] = {
            "nombre": entrada.nombre,
            "url": entrada.url,
            "registrado": time.time(),
        }
    return {"mensaje": f"Servicio '{entrada.nombre}' publicado", "url": entrada.url}


@app.get("/servicios", tags=["descubrimiento"])
def listar():
    """Devuelve el directorio completo."""
    metricas.peticiones += 1
    with _candado:
        return {"servicios": list(_servicios.values())}


@app.get("/servicios/{nombre}", tags=["descubrimiento"])
def resolver(nombre: str):
    """Resuelve un nombre en la URL de su servicio."""
    metricas.peticiones += 1
    with _candado:
        if nombre not in _servicios:
            metricas.errores += 1
            raise HTTPException(status_code=404, detail=f"No hay ningun servicio '{nombre}'")
        return _servicios[nombre]


@app.delete("/servicios/{nombre}", tags=["descubrimiento"])
def retirar(nombre: str, _clave: str = Depends(exigir_clave)):
    """Retira un servicio del directorio. Operacion protegida."""
    metricas.peticiones += 1
    with _candado:
        if _servicios.pop(nombre, None) is None:
            raise HTTPException(status_code=404, detail=f"No hay ningun servicio '{nombre}'")
    return {"mensaje": f"Servicio '{nombre}' retirado"}


# --- Capa transversal de gestion --------------------------------------------

@app.get("/health", tags=["gestion"])
def salud():
    return {"estado": "vivo", "servicio": "registro"}


@app.get("/metricas", tags=["gestion"])
def ver_metricas():
    return metricas.como_dict()


if __name__ == "__main__":
    print("=== SERVICIO DE DESCUBRIMIENTO ===")
    print(f"Documentacion OpenAPI: http://127.0.0.1:{PUERTOS['registro']}/docs\n")
    uvicorn.run(app, host="0.0.0.0", port=PUERTOS["registro"], log_level="warning")
