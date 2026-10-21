"""Servicio de catalogo.

Expone el mismo recurso de dos maneras para poder compararlas:

  - Estilo REST   : varios recursos, verbos de HTTP, JSON, codigos de estado
                    y control de concurrencia optimista con ETag / If-Match.
  - Estilo SOAP   : un unico endpoint, sobres XML, contrato WSDL y errores
                    dentro del cuerpo del mensaje (SOAP Fault).
"""

import threading
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Depends, Response, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from comun import (PUERTOS, url_de, permitir_origenes, exigir_clave, Metricas,
                   calcular_etag, verificar_if_match, registrar_servicio,
                   asegurar_registro)

ESPACIO_NOMBRES = "http://escom.ipn.mx/blockbuster"
SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"

app = FastAPI(
    title="Servicio de Catalogo",
    description=(
        "Catalogo de peliculas. Las operaciones de escritura emplean control "
        "de concurrencia optimista: el cliente debe enviar en If-Match el ETag "
        "que recibio al leer el recurso."
    ),
    version="1.0.0",
)
permitir_origenes(app)

metricas = Metricas("catalogo")
_candado = threading.Lock()


class Pelicula:
    def __init__(self, titulo: str):
        self.titulo = titulo
        self.disponible = True
        self.rentada_por = None
        self.version = 1

    def como_dict(self) -> dict:
        return {
            "titulo": self.titulo,
            "disponible": self.disponible,
            "rentada_por": self.rentada_por,
            "version": self.version,
        }

    def etag(self) -> str:
        return calcular_etag(self.como_dict())


_catalogo = {
    titulo: Pelicula(titulo)
    for titulo in ("Jurassic Park", "The Matrix", "Back to the Future")
}


def _buscar(titulo: str) -> Pelicula:
    pelicula = _catalogo.get(titulo)
    if pelicula is None:
        raise HTTPException(status_code=404, detail=f"'{titulo}' no existe en el catalogo")
    return pelicula


class SolicitudRenta(BaseModel):
    cliente: int = Field(..., description="Identificador del cliente", examples=[3])


# ===========================================================================
#  Estilo REST
# ===========================================================================

@app.get("/catalogo", tags=["catalogo (REST)"])
def consultar_catalogo(response: Response):
    """Devuelve el catalogo completo con un ETag del conjunto."""
    metricas.peticiones += 1
    with _candado:
        peliculas = [p.como_dict() for p in _catalogo.values()]
    response.headers["ETag"] = calcular_etag(peliculas)
    return {"peliculas": peliculas}


@app.get("/catalogo/{titulo}", tags=["catalogo (REST)"])
def consultar_pelicula(titulo: str, response: Response):
    """Devuelve una pelicula y el ETag que debera usarse para modificarla."""
    metricas.peticiones += 1
    with _candado:
        pelicula = _buscar(titulo)
        datos, etag = pelicula.como_dict(), pelicula.etag()
    response.headers["ETag"] = etag
    return datos


@app.post("/catalogo/{titulo}/rentar", tags=["catalogo (REST)"],
          dependencies=[Depends(exigir_clave)])
def rentar(titulo: str, solicitud: SolicitudRenta, response: Response,
           if_match: str = Header(default=None, alias="If-Match")):
    """Renta una pelicula si nadie la modifico desde que el cliente la leyo.

    Respuestas posibles:
      200  renta concedida
      404  la pelicula no existe
      409  el estado no permite la operacion (ya estaba rentada)
      412  el recurso cambio desde la lectura (ETag distinto)
      428  falta la cabecera If-Match
    """
    metricas.peticiones += 1
    with _candado:
        pelicula = _buscar(titulo)

        # Concurrencia optimista: no se bloquea, se comprueba
        try:
            verificar_if_match(if_match, pelicula.etag())
        except HTTPException:
            metricas.conflictos += 1
            raise

        if not pelicula.disponible:
            metricas.conflictos += 1
            raise HTTPException(
                status_code=409,
                detail=f"'{titulo}' ya esta rentada por el cliente {pelicula.rentada_por}")

        pelicula.disponible = False
        pelicula.rentada_por = solicitud.cliente
        pelicula.version += 1
        etag = pelicula.etag()

    response.headers["ETag"] = etag
    return {"mensaje": f"Renta de '{titulo}' concedida al cliente {solicitud.cliente}",
            "pelicula": pelicula.como_dict()}


@app.post("/catalogo/{titulo}/devolver", tags=["catalogo (REST)"],
          dependencies=[Depends(exigir_clave)])
def devolver(titulo: str, solicitud: SolicitudRenta, response: Response,
             if_match: str = Header(default=None, alias="If-Match")):
    """Devuelve una pelicula previamente rentada por el mismo cliente."""
    metricas.peticiones += 1
    with _candado:
        pelicula = _buscar(titulo)

        try:
            verificar_if_match(if_match, pelicula.etag())
        except HTTPException:
            metricas.conflictos += 1
            raise

        if pelicula.disponible:
            metricas.conflictos += 1
            raise HTTPException(status_code=409, detail=f"'{titulo}' no estaba rentada")
        if pelicula.rentada_por != solicitud.cliente:
            raise HTTPException(
                status_code=403,
                detail=f"'{titulo}' fue rentada por el cliente {pelicula.rentada_por}")

        pelicula.disponible = True
        pelicula.rentada_por = None
        pelicula.version += 1
        etag = pelicula.etag()

    response.headers["ETag"] = etag
    return {"mensaje": f"Devolucion de '{titulo}' registrada",
            "pelicula": pelicula.como_dict()}


# ===========================================================================
#  Estilo SOAP: un solo endpoint, sobres XML y contrato WSDL
# ===========================================================================

def _sobre(cuerpo: str) -> Response:
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<soap:Envelope xmlns:soap="{SOAP_ENV}">\n'
           f'  <soap:Body>\n{cuerpo}\n  </soap:Body>\n'
           f'</soap:Envelope>')
    return Response(content=xml, media_type="text/xml; charset=utf-8")


def _fault(codigo: str, mensaje: str) -> Response:
    """En SOAP el error viaja DENTRO del cuerpo, con codigo HTTP 200."""
    cuerpo = (f'    <soap:Fault>\n'
              f'      <faultcode>{codigo}</faultcode>\n'
              f'      <faultstring>{escape(mensaje)}</faultstring>\n'
              f'    </soap:Fault>')
    return _sobre(cuerpo)


@app.post("/soap", tags=["catalogo (SOAP)"],
          summary="Endpoint unico del servicio SOAP")
async def punto_soap(request: Request):
    """Acepta ConsultarCatalogo y Rentar dentro de un sobre SOAP."""
    metricas.peticiones += 1
    crudo = await request.body()

    try:
        arbol = ET.fromstring(crudo)
    except ET.ParseError as error:
        return _fault("soap:Client", f"XML mal formado: {error}")

    cuerpo = arbol.find(f"{{{SOAP_ENV}}}Body")
    if cuerpo is None or len(cuerpo) == 0:
        return _fault("soap:Client", "El sobre no contiene un Body con operacion")

    operacion = cuerpo[0]
    nombre = operacion.tag.split("}")[-1]

    if nombre == "ConsultarCatalogo":
        with _candado:
            filas = "".join(
                f'      <Pelicula>\n'
                f'        <Titulo>{escape(p.titulo)}</Titulo>\n'
                f'        <Disponible>{"true" if p.disponible else "false"}</Disponible>\n'
                f'      </Pelicula>\n'
                for p in _catalogo.values())
        return _sobre(f'    <ConsultarCatalogoResponse xmlns="{ESPACIO_NOMBRES}">\n'
                      f'{filas}    </ConsultarCatalogoResponse>')

    if nombre == "Rentar":
        titulo = operacion.findtext(f"{{{ESPACIO_NOMBRES}}}Titulo") or operacion.findtext("Titulo")
        cliente = operacion.findtext(f"{{{ESPACIO_NOMBRES}}}Cliente") or operacion.findtext("Cliente")

        if not titulo or not cliente:
            return _fault("soap:Client", "Faltan los elementos Titulo o Cliente")

        with _candado:
            pelicula = _catalogo.get(titulo)
            if pelicula is None:
                metricas.errores += 1
                return _fault("soap:Client", f"'{titulo}' no existe en el catalogo")
            if not pelicula.disponible:
                metricas.conflictos += 1
                return _fault("soap:Server", f"'{titulo}' ya esta rentada")

            pelicula.disponible = False
            pelicula.rentada_por = int(cliente)
            pelicula.version += 1

        return _sobre(f'    <RentarResponse xmlns="{ESPACIO_NOMBRES}">\n'
                      f'      <Resultado>Renta de {escape(titulo)} concedida</Resultado>\n'
                      f'    </RentarResponse>')

    return _fault("soap:Client", f"Operacion desconocida: {nombre}")


@app.get("/servicio.wsdl", tags=["catalogo (SOAP)"],
         summary="Contrato WSDL del servicio SOAP")
def wsdl():
    """Descripcion formal del servicio, equivalente al OpenAPI de /docs."""
    destino = f"{url_de('catalogo')}/soap"
    documento = f"""<?xml version="1.0" encoding="UTF-8"?>
<definitions name="CatalogoService"
             targetNamespace="{ESPACIO_NOMBRES}"
             xmlns="http://schemas.xmlsoap.org/wsdl/"
             xmlns:tns="{ESPACIO_NOMBRES}"
             xmlns:xsd="http://www.w3.org/2001/XMLSchema"
             xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/">

  <types>
    <xsd:schema targetNamespace="{ESPACIO_NOMBRES}">
      <xsd:element name="ConsultarCatalogo"><xsd:complexType/></xsd:element>
      <xsd:element name="ConsultarCatalogoResponse">
        <xsd:complexType><xsd:sequence>
          <xsd:element name="Pelicula" minOccurs="0" maxOccurs="unbounded">
            <xsd:complexType><xsd:sequence>
              <xsd:element name="Titulo" type="xsd:string"/>
              <xsd:element name="Disponible" type="xsd:boolean"/>
            </xsd:sequence></xsd:complexType>
          </xsd:element>
        </xsd:sequence></xsd:complexType>
      </xsd:element>
      <xsd:element name="Rentar">
        <xsd:complexType><xsd:sequence>
          <xsd:element name="Titulo" type="xsd:string"/>
          <xsd:element name="Cliente" type="xsd:int"/>
        </xsd:sequence></xsd:complexType>
      </xsd:element>
      <xsd:element name="RentarResponse">
        <xsd:complexType><xsd:sequence>
          <xsd:element name="Resultado" type="xsd:string"/>
        </xsd:sequence></xsd:complexType>
      </xsd:element>
    </xsd:schema>
  </types>

  <message name="ConsultarCatalogoIn"><part name="parametros" element="tns:ConsultarCatalogo"/></message>
  <message name="ConsultarCatalogoOut"><part name="parametros" element="tns:ConsultarCatalogoResponse"/></message>
  <message name="RentarIn"><part name="parametros" element="tns:Rentar"/></message>
  <message name="RentarOut"><part name="parametros" element="tns:RentarResponse"/></message>

  <portType name="CatalogoPortType">
    <operation name="ConsultarCatalogo">
      <input message="tns:ConsultarCatalogoIn"/>
      <output message="tns:ConsultarCatalogoOut"/>
    </operation>
    <operation name="Rentar">
      <input message="tns:RentarIn"/>
      <output message="tns:RentarOut"/>
    </operation>
  </portType>

  <binding name="CatalogoBinding" type="tns:CatalogoPortType">
    <soap:binding style="document" transport="http://schemas.xmlsoap.org/soap/http"/>
    <operation name="ConsultarCatalogo">
      <soap:operation soapAction="{ESPACIO_NOMBRES}/ConsultarCatalogo"/>
      <input><soap:body use="literal"/></input>
      <output><soap:body use="literal"/></output>
    </operation>
    <operation name="Rentar">
      <soap:operation soapAction="{ESPACIO_NOMBRES}/Rentar"/>
      <input><soap:body use="literal"/></input>
      <output><soap:body use="literal"/></output>
    </operation>
  </binding>

  <service name="CatalogoService">
    <port name="CatalogoPort" binding="tns:CatalogoBinding">
      <soap:address location="{destino}"/>
    </port>
  </service>
</definitions>"""
    return Response(content=documento, media_type="text/xml; charset=utf-8")


# --- Capa transversal de gestion --------------------------------------------

@app.get("/health", tags=["gestion"])
def salud():
    asegurar_registro("catalogo", url_de("catalogo"))
    return {"estado": "vivo", "servicio": "catalogo"}


@app.get("/metricas", tags=["gestion"])
def ver_metricas():
    return metricas.como_dict()


@app.on_event("startup")
def al_arrancar():
    registrar_servicio("catalogo", url_de("catalogo"))


if __name__ == "__main__":
    print("=== SERVICIO DE CATALOGO ===")
    print(f"OpenAPI : http://127.0.0.1:{PUERTOS['catalogo']}/docs")
    print(f"WSDL    : http://127.0.0.1:{PUERTOS['catalogo']}/servicio.wsdl\n")
    uvicorn.run(app, host="0.0.0.0", port=PUERTOS["catalogo"], log_level="warning")
