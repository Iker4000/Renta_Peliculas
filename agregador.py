"""Servicio agregador (capa de Procesos).

Demuestra las tres ideas de la capa superior de la pila:

  Descubrimiento  consulta el registro para saber que servicios existen
  Agregacion      combina en una sola respuesta datos de varios servicios
  Coreografia     ejecuta una secuencia de invocaciones entre servicios
"""

import uvicorn
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field

from comun import (PUERTOS, url_de, permitir_origenes, exigir_clave, Metricas,
                   ClienteServicio, CLAVE_API, registrar_servicio)

app = FastAPI(
    title="Servicio Agregador",
    description="Compone los servicios de catalogo y rentas en operaciones de negocio.",
    version="1.0.0",
)
permitir_origenes(app)

metricas = Metricas("agregador")
catalogo = ClienteServicio("catalogo")
rentas = ClienteServicio("rentas")
registro = ClienteServicio("registro")


class SolicitudFlujo(BaseModel):
    cliente: int = Field(..., description="Identificador del cliente", examples=[7])


# --- Descubrimiento ---------------------------------------------------------

@app.get("/servicios", tags=["descubrimiento"])
def servicios_disponibles():
    """Consulta el registro: que servicios hay y donde estan."""
    metricas.peticiones += 1
    try:
        return registro.get("/servicios").json()
    except Exception:
        metricas.errores += 1
        raise HTTPException(status_code=503, detail="El registro no responde")


# --- Agregacion -------------------------------------------------------------

@app.get("/vista", tags=["agregacion"])
def vista_unificada():
    """Una sola peticion del cliente, dos invocaciones internas.

    El consumidor recibe el catalogo y las rentas combinados, sin tener que
    conocer ni contactar a cada servicio por separado.
    """
    metricas.peticiones += 1
    try:
        peliculas = catalogo.get("/catalogo").json()["peliculas"]
        activas = rentas.get("/rentas").json()["rentas"]
    except Exception:
        metricas.errores += 1
        raise HTTPException(status_code=503, detail="Alguna dependencia no responde")

    por_titulo = {r["titulo"]: r for r in activas}
    combinado = []
    for pelicula in peliculas:
        renta = por_titulo.get(pelicula["titulo"])
        combinado.append({
            **pelicula,
            "renta_id": renta["id"] if renta else None,
            "reintentos_de_la_renta": renta["reintentos"] if renta else None,
        })

    return {
        "peliculas": combinado,
        "resumen": {
            "total": len(combinado),
            "disponibles": sum(1 for p in combinado if p["disponible"]),
            "rentadas": sum(1 for p in combinado if not p["disponible"]),
        },
    }


# --- Coreografia ------------------------------------------------------------

@app.post("/flujo-renta", tags=["coreografia"],
          dependencies=[Depends(exigir_clave)])
def flujo_renta(solicitud: SolicitudFlujo):
    """Secuencia completa: consultar, elegir, rentar y confirmar.

    Cada paso se registra para que la traza del proceso de negocio sea
    visible, que es lo que la capa de Procesos pretende describir.
    """
    metricas.peticiones += 1
    pasos = []

    # Paso 1: consultar el catalogo
    respuesta = catalogo.get("/catalogo")
    peliculas = respuesta.json()["peliculas"]
    pasos.append({"paso": 1, "accion": "GET catalogo/catalogo",
                  "resultado": f"{len(peliculas)} peliculas, ETag {respuesta.headers.get('ETag')}"})

    # Paso 2: elegir una disponible
    disponibles = [p["titulo"] for p in peliculas if p["disponible"]]
    if not disponibles:
        pasos.append({"paso": 2, "accion": "elegir", "resultado": "ninguna disponible"})
        raise HTTPException(status_code=409,
                            detail={"mensaje": "No hay peliculas disponibles", "pasos": pasos})
    elegida = disponibles[0]
    pasos.append({"paso": 2, "accion": "elegir", "resultado": elegida})

    # Paso 3: delegar la renta en el servicio correspondiente
    creacion = rentas.post("/rentas",
                           json={"cliente": solicitud.cliente, "titulo": elegida},
                           headers={"X-API-Key": CLAVE_API})
    if creacion.status_code != 201:
        pasos.append({"paso": 3, "accion": "POST rentas/rentas",
                      "resultado": f"fallo {creacion.status_code}"})
        raise HTTPException(status_code=creacion.status_code,
                            detail={"mensaje": "La renta no prospero", "pasos": pasos})

    cuerpo = creacion.json()
    pasos.append({"paso": 3, "accion": "POST rentas/rentas",
                  "resultado": f"renta {cuerpo['renta']['id']} en {cuerpo['intentos']} intento(s)"})

    # Paso 4: confirmar contra el catalogo
    confirmacion = catalogo.get(f"/catalogo/{elegida}").json()
    pasos.append({"paso": 4, "accion": f"GET catalogo/catalogo/{elegida}",
                  "resultado": f"version {confirmacion['version']}, "
                               f"rentada por {confirmacion['rentada_por']}"})

    return {"mensaje": f"Flujo completado para el cliente {solicitud.cliente}",
            "pelicula": elegida, "renta": cuerpo["renta"], "pasos": pasos}


# --- Capa transversal de gestion --------------------------------------------

@app.get("/health", tags=["gestion"])
def salud():
    """Salud propia y de las dependencias."""
    estado = {}
    for nombre, cliente in (("catalogo", catalogo), ("rentas", rentas), ("registro", registro)):
        try:
            estado[nombre] = cliente.get("/health").status_code == 200
        except Exception:
            estado[nombre] = False
    return {"estado": "vivo" if all(estado.values()) else "degradado",
            "servicio": "agregador", "dependencias": estado}


@app.get("/metricas", tags=["gestion"])
def ver_metricas():
    return metricas.como_dict()


@app.on_event("startup")
def al_arrancar():
    registrar_servicio("agregador", url_de("agregador"))


if __name__ == "__main__":
    print("=== SERVICIO AGREGADOR ===")
    print(f"OpenAPI: http://127.0.0.1:{PUERTOS['agregador']}/docs\n")
    uvicorn.run(app, host="0.0.0.0", port=PUERTOS["agregador"], log_level="warning")
