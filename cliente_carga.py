"""Cliente de carga: provoca conflictos de concurrencia optimista.

Lanza varios clientes simultaneos contra el mismo catalogo para que la
ventana entre la lectura del ETag y la escritura se solape y el servidor
tenga que rechazar operaciones con 412. Mide cuantos conflictos ocurren.
"""

import os
import sys
import time
import random
import threading

import httpx

from comun import url_de, CLAVE_API

CLIENTES = 6
CICLOS = 5
LAMBDA = 1.0        # llegadas de Poisson, media 1 s entre ciclos

# Misma ventana que usa el servicio de rentas, para que la comparacion
# aisle el efecto del reintento y no el del tamano de la ventana.
VENTANA_MEDIA = float(os.environ.get("VENTANA_SEGUNDOS", "0"))

_candado = threading.Lock()
_resultados = []


def traza(mensaje: str):
    with _candado:
        print(mensaje, flush=True)


def un_cliente(cliente_id: int, directo: bool):
    """Si directo, habla con el catalogo y gestiona el conflicto por su cuenta;
    si no, delega en el servicio de rentas, que ya reintenta."""
    base_catalogo = url_de("catalogo")
    base_rentas = url_de("rentas")
    cabeceras = {"X-API-Key": CLAVE_API}

    with httpx.Client(timeout=10.0) as http:
        for ciclo in range(1, CICLOS + 1):
            time.sleep(random.expovariate(LAMBDA))

            catalogo = http.get(f"{base_catalogo}/catalogo").json()["peliculas"]
            disponibles = [p["titulo"] for p in catalogo if p["disponible"]]
            if not disponibles:
                traza(f"[Cliente {cliente_id}] ciclo {ciclo}: sin peliculas disponibles")
                continue

            elegida = random.choice(disponibles)
            inicio = time.time()

            if directo:
                # Lectura del ETag y escritura condicionada, sin reintentos
                lectura = http.get(f"{base_catalogo}/catalogo/{elegida}")
                etag = lectura.headers.get("ETag")

                # Ventana entre lectura y escritura: mientras dure, otro
                # cliente puede adelantarse e invalidar el ETag leido
                if VENTANA_MEDIA:
                    time.sleep(random.expovariate(1 / VENTANA_MEDIA))

                respuesta = http.post(
                    f"{base_catalogo}/catalogo/{elegida}/rentar",
                    json={"cliente": cliente_id},
                    headers={**cabeceras, "If-Match": etag})
                codigo = respuesta.status_code
                intentos = 1
            else:
                respuesta = http.post(
                    f"{base_rentas}/rentas",
                    json={"cliente": cliente_id, "titulo": elegida},
                    headers=cabeceras)
                codigo = respuesta.status_code
                intentos = respuesta.json().get("intentos", 1) if codigo == 201 else 1

            duracion = time.time() - inicio
            with _candado:
                _resultados.append({"codigo": codigo, "intentos": intentos,
                                    "duracion": duracion})

            if codigo in (200, 201):
                traza(f"[Cliente {cliente_id}] ciclo {ciclo}: RENTO '{elegida}' "
                      f"({intentos} intento/s, {duracion:.3f} s)")
                time.sleep(random.expovariate(1.2))

                if directo:
                    lectura = http.get(f"{base_catalogo}/catalogo/{elegida}")
                    http.post(f"{base_catalogo}/catalogo/{elegida}/devolver",
                              json={"cliente": cliente_id},
                              headers={**cabeceras, "If-Match": lectura.headers.get("ETag")})
                else:
                    identificador = respuesta.json()["renta"]["id"]
                    http.request("DELETE", f"{base_rentas}/rentas/{identificador}",
                                 headers=cabeceras)
            elif codigo == 412:
                traza(f"[Cliente {cliente_id}] ciclo {ciclo}: 412 el catalogo cambio "
                      f"mientras decidia sobre '{elegida}'")
            elif codigo == 409:
                traza(f"[Cliente {cliente_id}] ciclo {ciclo}: 409 '{elegida}' ya estaba rentada")
            else:
                traza(f"[Cliente {cliente_id}] ciclo {ciclo}: codigo {codigo}")


if __name__ == "__main__":
    directo = "--directo" in sys.argv
    modo = ("SIN reintentos, contra el catalogo" if directo
            else "CON reintentos, a traves del servicio de rentas")

    print(f"=== CLIENTE DE CARGA ({CLIENTES} clientes, {CICLOS} ciclos) ===")
    print(f"Modo: {modo}\n")

    hilos = [threading.Thread(target=un_cliente, args=(i, directo))
             for i in range(1, CLIENTES + 1)]
    inicio = time.time()
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()
    duracion = time.time() - inicio

    exitos = [r for r in _resultados if r["codigo"] in (200, 201)]
    conflictos = [r for r in _resultados if r["codigo"] in (409, 412)]
    total_intentos = sum(r["intentos"] for r in _resultados)

    print(f"\n=== RESULTADOS ({duracion:.1f} s) ===")
    print(f"Operaciones solicitadas   : {len(_resultados)}")
    print(f"Rentas concedidas         : {len(exitos)}")
    print(f"Conflictos (409/412)      : {len(conflictos)}")
    if _resultados:
        print(f"Tasa de conflicto         : {100 * len(conflictos) / len(_resultados):.1f} %")
    if exitos:
        print(f"Intentos por renta exitosa: {total_intentos / len(exitos):.2f}")
        print(f"Latencia media            : {sum(r['duracion'] for r in exitos) / len(exitos):.3f} s")
