# Práctica 6 — Servicios Web

Sistema de rentas Blockbuster implementado como una arquitectura de servicios,
recorriendo las capas de la pila de servicios web: comunicaciones, mensajes,
descripción y procesos, más seguridad y gestión como capas transversales.

**Sistemas Distribuidos · 7CM1 · ESCOM-IPN**

---

## Arquitectura

```
                    Frontend (GitHub Pages)
                              |
                              v
                       Agregador  :8003
                      /           \
          Catálogo :8001  <----  Rentas :8002
                      \           /
                       Registro  :8000
                    (descubrimiento)
```

| Servicio | Puerto | Responsabilidad |
|---|---|---|
| `registro.py` | 8000 | Descubrimiento: traduce nombres en URLs |
| `catalogo.py` | 8001 | Catálogo de películas. REST con ETag **y** endpoint SOAP |
| `rentas.py` | 8002 | Rentas. Compone llamadas al catálogo y reintenta ante conflictos |
| `agregador.py` | 8003 | Agregación y coreografía de los anteriores |

`comun.py` reúne lo transversal: configuración, CORS, API key, ETags y el
cliente HTTP entre servicios. No se ejecuta por sí solo.

## Cómo se mapea la pila de servicios web

| Capa | Dónde está en el código |
|---|---|
| **Procesos** (descubrimiento, agregación, coreografía) | `registro.py`, `agregador.py` |
| **Descripción** (JSON, WSDL, XML) | OpenAPI en `/docs`, contrato en `/servicio.wsdl` |
| **Mensajes** (SOAP, GET/POST) | Endpoints REST y `/soap` en `catalogo.py` |
| **Comunicaciones** (HTTP) | FastAPI + uvicorn, sobre TCP/IP |
| **Seguridad** ⇕ | `exigir_clave()` en `comun.py`, HTTPS en el despliegue |
| **Gestión** ⇕ | `/health` y `/metricas` en cada servicio |

## Ejecución local

```bash
pip install -r requirements.txt

# Una terminal por servicio, en este orden
python registro.py
python catalogo.py
python rentas.py
python agregador.py
```

Documentación interactiva generada automáticamente:

- <http://127.0.0.1:8001/docs> — catálogo
- <http://127.0.0.1:8002/docs> — rentas
- <http://127.0.0.1:8003/docs> — agregador
- <http://127.0.0.1:8001/servicio.wsdl> — contrato SOAP

El frontend está en `docs/index.html`: ábrelo en el navegador o publícalo
con GitHub Pages.

## Concurrencia optimista

Las operaciones de escritura exigen la cabecera `If-Match` con el `ETag`
obtenido al leer el recurso:

```bash
# 1. Leer y quedarse con el ETag
curl -i http://127.0.0.1:8001/catalogo/The%20Matrix

# 2. Escribir enviándolo
curl -X POST http://127.0.0.1:8001/catalogo/The%20Matrix/rentar \
     -H "Content-Type: application/json" \
     -H "X-API-Key: renta-peliculas" \
     -H 'If-Match: "2a7cce75dc62ea21"' \
     -d '{"cliente": 3}'
```

| Código | Significado |
|---|---|
| `200` | Operación concedida |
| `401` | Falta `X-API-Key` o es incorrecta |
| `403` | La película la rentó otro cliente |
| `404` | La película no existe |
| `409` | El estado no admite la operación (ya estaba rentada) |
| `412` | El recurso cambió desde la lectura: hay que releer y reintentar |
| `428` | Falta la cabecera `If-Match` |

## Prueba de carga

```bash
# Sin reintentos: el cliente habla directamente con el catálogo
VENTANA_SEGUNDOS=0.3 python cliente_carga.py --directo

# Con reintentos: el servicio de rentas absorbe los conflictos
VENTANA_SEGUNDOS=0.3 python cliente_carga.py
```

`VENTANA_SEGUNDOS` controla la separación entre la lectura del ETag y la
escritura. Cuanto mayor sea, más probable es el conflicto.

## Despliegue

**Backend en Render.** El archivo `render.yaml` es un blueprint que declara
los cuatro servicios e inyecta en cada uno las URLs de los demás. En Render se
entra a <https://dashboard.render.com/blueprints> → *New Blueprint*, se elige
este repositorio y se define `CLAVE_API` (el mismo valor en los cuatro).

Los servicios se comunican por sus **URLs públicas**, no por la red privada de
Render: en el plan gratuito un servicio puede *enviar* tráfico privado pero no
*recibirlo*, así que `fromService` con `property: hostport` dejaría al agregador
sin poder alcanzar al catálogo. Como Render deriva el dominio del nombre del
servicio y ese nombre es único en toda la plataforma, los nombres del blueprint
llevan un sufijo propio; si se cambia un nombre hay que cambiar también las
URLs que lo mencionan.

**Frontend en GitHub Pages.** En *Settings → Pages*, fuente «Deploy from a
branch», rama `main`, carpeta `/docs`. Después se introducen en la página las
URLs públicas de los servicios, o se pasan en el enlace:

```
https://<usuario>.github.io/<repo>/?catalogo=https://sd-catalogo-iker.onrender.com&agregador=https://sd-agregador-iker.onrender.com
```

### Limitaciones conocidas

- **El estado vive en memoria.** El catálogo es una estructura de Python, así
  que funciona con una instancia por servicio pero no sobrevive a un reinicio
  ni se comparte entre réplicas. Un sistema real usaría una base de datos.
- **La capa gratuita de Render duerme** tras 15 minutos de inactividad y tarda
  cerca de un minuto en responder a la primera petición.
- **CORS está abierto a cualquier origen** (`*`) para simplificar la práctica.
  En producción se restringiría al dominio del frontend.
