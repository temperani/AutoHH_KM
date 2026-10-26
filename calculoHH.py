from datetime import datetime, time, timedelta
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import sys
import threading
import time as time_module
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import TypedDict
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont


FATORES = {
    1: 0.1780,
    2: 0.1913,
    3: 0.2045,
    4: 0.2178,
    5: 0.2311,
    6: 0.2443,
    7: 0.2575,
    8: 0.2709,
    9: 0.2841,
}

FATORES_KM_VEICULO = {
    "Caminhonete": 0.003208,
    "Caminhão": 0.004735,
}


class PerfilCCSM(TypedDict):
    empresa: str
    responsavel: str
    supervisor: str


class DadosCCSM(TypedDict):
    destinatarios: list[str]
    perfil: PerfilCCSM
    nsmc: str
    tipo_vistoria: str
    municipio: str
    data_execucao: str
    alteracao_us: str
    total_us: str
    observacao: str


_cache_municipios: dict[tuple[float, float], str] = {}
_ultima_consulta_nominatim = 0.0
_lock_consulta_nominatim = threading.Lock()


def calcular_hh(
    acionamento: str,
    termino_real: str,
    descontar_almoco: bool,
    elementos: int,
) -> tuple[timedelta, float, float]:
    inicio = datetime.strptime(acionamento, "%H:%M")
    fim = datetime.strptime(termino_real, "%H:%M")

    if fim < inicio:
        fim += timedelta(days=1)

    tempo_trabalhado = fim - inicio
    if descontar_almoco:
        dia = inicio.date()
        while dia <= fim.date():
            almoco_inicio = datetime.combine(dia, time(12, 0))
            almoco_fim = datetime.combine(dia, time(13, 0))
            if inicio <= almoco_inicio and fim >= almoco_fim:
                tempo_trabalhado -= timedelta(hours=1)
            dia += timedelta(days=1)

    horas_trabalhadas = tempo_trabalhado.total_seconds() / 3600
    fator = FATORES[elementos]
    return tempo_trabalhado, fator, horas_trabalhadas * fator


def formatar_decimal(valor: float) -> str:
    return f"{valor:.2f}".replace(".", ",")


def distancia_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    raio_terra_km = 6371.0
    diferenca_lat = math.radians(lat2 - lat1)
    diferenca_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(diferenca_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(diferenca_lon / 2) ** 2
    )
    a = min(1.0, max(0.0, a))
    arco = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return raio_terra_km * arco


def calcular_km_veiculo(distancia_ida: float, tipo_veiculo: str) -> tuple[float, float]:
    try:
        fator = FATORES_KM_VEICULO[tipo_veiculo]
    except KeyError as erro:
        raise ValueError("Selecione um tipo de veículo válido.") from erro
    distancia_ida_e_volta = distancia_ida * 2
    return distancia_ida_e_volta, distancia_ida_e_volta * fator


def calcular_km_rota(
    origem: tuple[float, float],
    pontos: list[tuple[float, float]],
    tipo_veiculo: str,
) -> tuple[float, float]:
    if not pontos:
        raise ValueError("Adicione ao menos um ponto de atendimento.")
    try:
        fator = FATORES_KM_VEICULO[tipo_veiculo]
    except KeyError as erro:
        raise ValueError("Selecione um tipo de veículo válido.") from erro

    distancia_total = 0.0
    local_anterior = origem
    for ponto in pontos:
        distancia_total += distancia_km(
            local_anterior[0],
            local_anterior[1],
            ponto[0],
            ponto[1],
        )
        local_anterior = ponto
    distancia_total += distancia_km(
        local_anterior[0],
        local_anterior[1],
        origem[0],
        origem[1],
    )
    return distancia_total, distancia_total * fator


def ler_coordenada(valor: str, nome: str, minimo: float, maximo: float) -> float:
    try:
        coordenada = float(valor.strip().replace(",", "."))
    except ValueError as erro:
        raise ValueError(f"Informe uma {nome} válida.") from erro
    if not math.isfinite(coordenada) or not minimo <= coordenada <= maximo:
        raise ValueError(f"A {nome} deve estar entre {minimo} e {maximo}.")
    return coordenada


def caminho_dados_base() -> Path:
    pasta_dados = Path(os.environ.get("APPDATA", Path.home())) / "CalculadoraHH"
    return pasta_dados / "bases.json"


def carregar_bases() -> dict[str, dict[str, float]]:
    arquivo = caminho_dados_base()
    try:
        with arquivo.open("r", encoding="utf-8") as dados:
            bases = json.load(dados)
    except FileNotFoundError:
        return {}

    if not isinstance(bases, dict):
        raise ValueError("O arquivo de cadastro das bases possui formato inválido.")
    for empreiteira, coordenadas in bases.items():
        if (
            not isinstance(empreiteira, str)
            or not isinstance(coordenadas, dict)
            or not isinstance(coordenadas.get("latitude"), (int, float))
            or not isinstance(coordenadas.get("longitude"), (int, float))
        ):
            raise ValueError("O arquivo de cadastro das bases possui dados inválidos.")
        ler_coordenada(str(coordenadas["latitude"]), "latitude da base", -90, 90)
        ler_coordenada(str(coordenadas["longitude"]), "longitude da base", -180, 180)
    return bases


def salvar_bases(bases: dict[str, dict[str, float]]) -> None:
    arquivo = caminho_dados_base()
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    with arquivo.open("w", encoding="utf-8") as dados:
        json.dump(bases, dados, ensure_ascii=False, indent=2)


def caminho_perfis_ccsm() -> Path:
    pasta_dados = Path(os.environ.get("APPDATA", Path.home())) / "CalculadoraHH"
    return pasta_dados / "perfis_ccsm.json"


def carregar_perfis_ccsm() -> dict[str, PerfilCCSM]:
    arquivo = caminho_perfis_ccsm()
    try:
        with arquivo.open("r", encoding="utf-8") as dados:
            perfis = json.load(dados)
    except FileNotFoundError:
        return {}

    if not isinstance(perfis, dict):
        raise ValueError("O arquivo de perfis CCSM possui formato inválido.")
    for nome, perfil in perfis.items():
        if (
            not isinstance(nome, str)
            or not isinstance(perfil, dict)
            or any(
                not isinstance(perfil.get(campo), str)
                for campo in ("empresa", "responsavel", "supervisor")
            )
        ):
            raise ValueError("O arquivo de perfis CCSM possui dados inválidos.")
    return perfis


def salvar_perfis_ccsm(perfis: dict[str, PerfilCCSM]) -> None:
    arquivo = caminho_perfis_ccsm()
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    with arquivo.open("w", encoding="utf-8") as dados:
        json.dump(perfis, dados, ensure_ascii=False, indent=2)


def geocodificar_municipio(latitude: float, longitude: float) -> str:
    global _ultima_consulta_nominatim
    chave = (round(latitude, 7), round(longitude, 7))
    with _lock_consulta_nominatim:
        municipio_em_cache = _cache_municipios.get(chave)
        if municipio_em_cache:
            return municipio_em_cache
        espera = 1 - (time_module.monotonic() - _ultima_consulta_nominatim)
        if espera > 0:
            time_module.sleep(espera)
        _ultima_consulta_nominatim = time_module.monotonic()

    parametros = urlencode(
        {
            "lat": f"{chave[0]:.7f}",
            "lon": f"{chave[1]:.7f}",
            "format": "jsonv2",
            "addressdetails": 1,
            "zoom": 10,
        }
    )
    requisicao = Request(
        f"https://nominatim.openstreetmap.org/reverse?{parametros}",
        headers={"User-Agent": "CalculadoraHH/1.0 (geocodificacao CCSM)"},
    )
    try:
        with urlopen(requisicao, timeout=15) as resposta:
            resultado = json.loads(resposta.read().decode("utf-8"))
    except (
        URLError,
        HTTPException,
        TimeoutError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as erro:
        raise RuntimeError(
            "Não foi possível consultar o município online. Verifique a conexão "
            f"e tente novamente.\nDetalhe: {erro}"
        ) from erro

    if not isinstance(resultado, dict):
        raise RuntimeError("O serviço online retornou uma resposta inválida.")
    endereco = resultado.get("address")
    if not isinstance(endereco, dict):
        raise RuntimeError("O serviço online não identificou o município.")
    municipio = next(
        (
            endereco[campo]
            for campo in ("city", "town", "village", "municipality", "county")
            if isinstance(endereco.get(campo), str) and endereco[campo].strip()
        ),
        None,
    )
    if not municipio:
        raise RuntimeError(
            "A consulta online não encontrou o município para o primeiro ponto."
        )
    with _lock_consulta_nominatim:
        _cache_municipios[chave] = municipio
    return municipio


def _ajustar_texto_imagem(
    desenho: ImageDraw.ImageDraw,
    texto: str,
    x: float,
    y: float,
    largura_maxima: float,
    escala_x: float,
    escala_y: float,
    tamanho: float = 9,
) -> None:
    caminho_fonte = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"
    tamanho_px = max(1, round(tamanho * escala_y))
    fonte = ImageFont.truetype(str(caminho_fonte), tamanho_px)
    largura_px = largura_maxima * escala_x
    while (
        tamanho_px > 5
        and desenho.textbbox((0, 0), texto, font=fonte, anchor="ls")[2] > largura_px
    ):
        tamanho_px -= 1
        fonte = ImageFont.truetype(str(caminho_fonte), tamanho_px)
    desenho.text(
        (round(x * escala_x), round((842 - y) * escala_y)),
        texto,
        fill="black",
        font=fonte,
        anchor="ls",
    )


def _quebrar_texto_imagem(
    desenho: ImageDraw.ImageDraw,
    texto: str,
    largura_maxima_px: float,
    fonte: ImageFont.FreeTypeFont,
) -> list[str]:
    linhas: list[str] = []
    linha_atual = ""
    for palavra in texto.split():
        candidata = f"{linha_atual} {palavra}".strip()
        if (
            linha_atual
            and desenho.textbbox((0, 0), candidata, font=fonte)[2]
            > largura_maxima_px
        ):
            linhas.append(linha_atual)
            linha_atual = palavra
        else:
            linha_atual = candidata
    if linha_atual:
        linhas.append(linha_atual)
    return linhas


def gerar_imagem_ccsm(caminho_saida: str, dados: DadosCCSM) -> None:
    caminho_modelo = Path(
        getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
    ) / "CCSM_modelo.png"
    if not caminho_modelo.is_file():
        raise FileNotFoundError(f"Modelo CCSM não encontrado: {caminho_modelo}")

    with Image.open(caminho_modelo) as modelo:
        imagem = modelo.convert("RGBA")
    camada_texto = Image.new("RGBA", imagem.size, (0, 0, 0, 0))
    desenho = ImageDraw.Draw(camada_texto)
    escala_x = imagem.width / 595
    escala_y = imagem.height / 842

    def escrever(
        texto: str,
        x: float,
        y: float,
        largura_maxima: float,
        tamanho: float = 9,
    ) -> None:
        _ajustar_texto_imagem(
            desenho,
            texto,
            x,
            y,
            largura_maxima,
            escala_x,
            escala_y,
            tamanho,
        )

    destinatarios = ", ".join(dados["destinatarios"])
    escrever(destinatarios, 96, 750, 438)

    perfil = dados["perfil"]
    escrever(
        perfil["responsavel"],
        87,
        718,
        260,
    )
    escrever(
        perfil["empresa"],
        122,
        700,
        300,
    )

    escrever("X", 190, 654, 12)
    escrever(dados["nsmc"], 255, 654, 110)

    tipo_vistoria = str(dados["tipo_vistoria"])
    posicoes_vistoria = {1: 247, 2: 347, 3: 432}
    if tipo_vistoria.isdigit() and int(tipo_vistoria) in posicoes_vistoria:
        escrever("X", posicoes_vistoria[int(tipo_vistoria)], 632, 12, 9)

    escrever(
        dados["municipio"],
        105,
        614,
        185,
        9,
    )
    escrever(
        dados["data_execucao"],
        402,
        614,
        130,
        9,
    )
    alteracao_sim = dados["alteracao_us"] == "Sim"
    escrever("X", 170 if alteracao_sim else 245, 596, 12, 8.5)
    escrever(
        dados["total_us"],
        374,
        596,
        42,
        8,
    )
    escrever(
        dados["total_us"],
        526,
        596,
        42,
        8,
    )

    escrever("X", 67, 471, 12, 9)

    observacao = dados.get("observacao", "").strip()
    if observacao:
        tamanho_fonte = max(1, round(8 * escala_y))
        caminho_fonte = (
            Path(os.environ.get("WINDIR", r"C:\Windows"))
            / "Fonts"
            / "arial.ttf"
        )
        fonte_observacao = ImageFont.truetype(str(caminho_fonte), tamanho_fonte)
        linhas_observacao = _quebrar_texto_imagem(
            desenho,
            observacao,
            465 * escala_x,
            fonte_observacao,
        )
        if len(linhas_observacao) > 2:
            raise ValueError("A observação excede o espaço disponível no CCSM.")
        for indice, linha in enumerate(linhas_observacao):
            escrever(linha, 64, 182 - indice * 18, 465, 8)

    escrever(
        dados["data_execucao"],
        175,
        257,
        65,
        8.5,
    )
    escrever(dados["data_execucao"], 450, 257, 65, 8.5)
    escrever(perfil["responsavel"], 90, 282, 190, 10)
    escrever(perfil["supervisor"], 370, 282, 180, 10)

    imagem = Image.alpha_composite(imagem, camada_texto)
    imagem.save(caminho_saida, format="PNG")


def configurar_entrada_horario(entry: ttk.Entry) -> None:
    def ao_digitar(event: tk.Event) -> str | None:
        if event.char not in "0123456789" and event.keysym not in {
            "BackSpace",
            "Delete",
        }:
            return "break" if event.char and event.char.isprintable() else None

        valor = entry.get()
        cursor = entry.index(tk.INSERT)
        selecao = entry.selection_present()
        if selecao:
            inicio_selecao = entry.index(tk.SEL_FIRST)
            fim_selecao = entry.index(tk.SEL_LAST)
            inicio_digitos = sum(char.isdigit() for char in valor[:inicio_selecao])
            fim_digitos = sum(char.isdigit() for char in valor[:fim_selecao])
        else:
            inicio_digitos = sum(char.isdigit() for char in valor[:cursor])
            fim_digitos = inicio_digitos

        digitos = "".join(char for char in valor if char.isdigit())
        if selecao:
            digitos = digitos[:inicio_digitos] + digitos[fim_digitos:]

        if event.keysym == "BackSpace":
            if not selecao and inicio_digitos > 0:
                inicio_digitos -= 1
                digitos = digitos[:inicio_digitos] + digitos[inicio_digitos + 1:]
            novo_cursor = inicio_digitos
        elif event.keysym == "Delete":
            if not selecao and inicio_digitos < len(digitos):
                digitos = digitos[:inicio_digitos] + digitos[inicio_digitos + 1:]
            novo_cursor = inicio_digitos
        else:
            if len(digitos) >= 4:
                return "break"
            digitos = (
                digitos[:inicio_digitos]
                + event.char
                + digitos[inicio_digitos:]
            )
            novo_cursor = inicio_digitos + 1

        digitos = digitos[:4]
        formatado = digitos[:2]
        if len(digitos) > 2:
            formatado += ":" + digitos[2:]

        entry.delete(0, tk.END)
        entry.insert(0, formatado)
        posicao_cursor = novo_cursor + (1 if novo_cursor >= 2 and len(digitos) > 2 else 0)
        entry.icursor(posicao_cursor)
        return "break"

    entry.bind("<KeyPress>", ao_digitar)


def criar_janela() -> None:
    root = tk.Tk()
    root.title("Calculadora de H.H e KM Linear")
    root.state("zoomed")
    root.resizable(True, True)
    root.configure(background="#f3f6fb")

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TFrame", background="#f3f6fb")
    style.configure("Card.TFrame", background="#ffffff")
    style.configure(
        "Title.TLabel",
        background="#f3f6fb",
        foreground="#172b4d",
        font=("Segoe UI", 22, "bold"),
    )
    style.configure(
        "Subtitle.TLabel",
        background="#f3f6fb",
        foreground="#5e6c84",
        font=("Segoe UI", 10),
    )
    style.configure(
        "Section.TLabel",
        background="#ffffff",
        foreground="#172b4d",
        font=("Segoe UI", 12, "bold"),
    )
    style.configure(
        "Field.TLabel",
        background="#ffffff",
        foreground="#344563",
        font=("Segoe UI", 10),
    )
    style.configure(
        "ResultTitle.TLabel",
        background="#ffffff",
        foreground="#5e6c84",
        font=("Segoe UI", 9),
    )
    style.configure(
        "ResultValue.TLabel",
        background="#ffffff",
        foreground="#0052cc",
        font=("Segoe UI", 18, "bold"),
    )
    style.configure(
        "TEntry",
        padding=8,
        fieldbackground="#ffffff",
        font=("Segoe UI", 11),
    )
    style.configure(
        "TCombobox",
        padding=7,
        fieldbackground="#ffffff",
        font=("Segoe UI", 11),
    )
    style.configure(
        "Calculate.TButton",
        background="#0052cc",
        foreground="#ffffff",
        font=("Segoe UI", 10, "bold"),
        padding=(14, 10),
    )
    style.map(
        "Calculate.TButton",
        background=[("active", "#0747a6"), ("pressed", "#0747a6")],
    )
    style.configure(
        "Clear.TButton",
        background="#ebecf0",
        foreground="#172b4d",
        font=("Segoe UI", 10),
        padding=(14, 10),
    )

    layout = ttk.Frame(root)
    layout.pack(fill="both", expand=True)
    canvas = tk.Canvas(layout, background="#f3f6fb", highlightthickness=0)
    scrollbar = ttk.Scrollbar(layout, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    container = ttk.Frame(canvas, padding=(32, 28))
    canvas_window = canvas.create_window((0, 0), window=container, anchor="nw")

    def atualizar_area_rolagem(event: tk.Event) -> None:
        del event
        canvas.configure(scrollregion=canvas.bbox("all"))

    def ajustar_largura_conteudo(event: tk.Event) -> None:
        canvas.itemconfigure(canvas_window, width=event.width)

    container.bind("<Configure>", atualizar_area_rolagem)
    canvas.bind("<Configure>", ajustar_largura_conteudo)

    def rolar_conteudo(event: tk.Event) -> None:
        canvas.yview_scroll(int(-event.delta / 120), "units")

    canvas.bind_all("<MouseWheel>", rolar_conteudo)

    try:
        bases = carregar_bases()
        perfis_ccsm = carregar_perfis_ccsm()
    except (OSError, ValueError, json.JSONDecodeError) as erro:
        messagebox.showerror(
            "Erro ao carregar cadastros",
            f"Não foi possível carregar os cadastros locais:\n{erro}",
            parent=root,
        )
        root.destroy()
        return

    ttk.Label(container, text="Calculadora de H.H e KM Linear", style="Title.TLabel").pack(
        anchor="w"
    )
    ttk.Label(
        container,
        text="Calcule as horas-homem e a distância linear do atendimento.",
        style="Subtitle.TLabel",
    ).pack(anchor="w", pady=(4, 20))

    form = ttk.Frame(container, style="Card.TFrame", padding=22)
    form.pack(fill="x")
    ttk.Label(form, text="Dados do atendimento", style="Section.TLabel").grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 18)
    )

    ttk.Label(form, text="Horário do acionamento", style="Field.TLabel").grid(
        row=1, column=0, sticky="w", pady=(0, 6)
    )
    ttk.Label(form, text="Horário do término real", style="Field.TLabel").grid(
        row=1, column=1, sticky="w", padx=(18, 0), pady=(0, 6)
    )
    acionamento_entry = ttk.Entry(form, width=22)
    acionamento_entry.grid(row=2, column=0, sticky="ew")
    termino_entry = ttk.Entry(form, width=22)
    termino_entry.grid(row=2, column=1, sticky="ew", padx=(18, 0))
    configurar_entrada_horario(acionamento_entry)
    configurar_entrada_horario(termino_entry)
    acionamento_entry.insert(0, "")
    termino_entry.insert(0, "")

    ttk.Label(form, text="Número de elementos", style="Field.TLabel").grid(
        row=3, column=0, sticky="w", pady=(18, 6)
    )
    elementos_combo = ttk.Combobox(
        form,
        values=tuple(str(numero) for numero in FATORES),
        state="readonly",
        width=20,
    )
    elementos_combo.grid(row=4, column=0, sticky="ew")
    elementos_combo.set("1")
    validar_nota = root.register(
        lambda valor: not valor or (valor.isascii() and valor.isdigit())
    )
    campos_notas = ttk.Frame(form, style="Card.TFrame")
    campos_notas.grid(row=3, column=1, rowspan=2, sticky="ew", padx=(18, 0), pady=(18, 0))
    campos_nsri_ns_mc: list[ttk.Entry] = []
    for coluna, identificador in enumerate(("NSRI", "NSMC")):
        campo = ttk.Frame(campos_notas, style="Card.TFrame")
        campo.grid(row=0, column=coluna, sticky="ew", padx=(0 if coluna == 0 else 8, 0))
        ttk.Label(campo, text=identificador, style="Field.TLabel").pack(
            anchor="w", pady=(0, 6)
        )
        entrada = ttk.Entry(
            campo,
            width=10,
            validate="key",
            validatecommand=(validar_nota, "%P"),
        )
        entrada.pack(fill="x", expand=True)
        campos_notas.columnconfigure(coluna, weight=1)
        campos_nsri_ns_mc.append(entrada)

    descontar_almoco_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(
        form,
        text="Descontar hora de almoço",
        variable=descontar_almoco_var,
    ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(14, 0))
    form.columnconfigure(0, weight=1)
    form.columnconfigure(1, weight=1)

    arquivos_anexados: dict[str, str] = {}
    rotulos_anexos: dict[str, ttk.Label] = {}

    def selecionar_anexo(tipo: str) -> None:
        caminho = filedialog.askopenfilename(
            parent=root,
            title=f"Selecionar {tipo}",
            filetypes=(
                ("Documentos e imagens", "*.pdf *.png *.jpg *.jpeg *.doc *.docx"),
                ("Todos os arquivos", "*.*"),
            ),
        )
        if not caminho:
            return
        arquivos_anexados[tipo] = caminho
        rotulos_anexos[tipo].configure(text=Path(caminho).name)

    coordenadas = ttk.Frame(container, style="Card.TFrame", padding=22)
    coordenadas.pack(fill="x", pady=(18, 0))
    ttk.Label(
        coordenadas, text="Distância linear (km)", style="Section.TLabel"
    ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 14))

    ttk.Label(coordenadas, text="Tipo de veículo", style="Field.TLabel").grid(
        row=1, column=0, sticky="w", pady=(0, 6)
    )
    partindo_base_var = tk.BooleanVar(value=False)
    veiculo_combo = ttk.Combobox(
        coordenadas,
        values=("Caminhonete", "Caminhão"),
        state="readonly",
    )
    veiculo_combo.grid(row=2, column=0, sticky="ew")
    ttk.Label(coordenadas, text="Empreiteira / base", style="Field.TLabel").grid(
        row=3, column=0, sticky="w", pady=(14, 6)
    )
    empreiteira_combo = ttk.Combobox(
        coordenadas,
        values=tuple(sorted(bases)),
        state="disabled",
    )
    empreiteira_combo.grid(row=4, column=0, sticky="ew")
    controles_base = ttk.Frame(coordenadas, style="Card.TFrame")
    controles_base.grid(row=4, column=1, sticky="w", padx=(10, 0))
    cadastrar_base_button = ttk.Button(
        controles_base,
        text="Cadastrar coordenada da base",
    )
    cadastrar_base_button.pack(side="left")
    ttk.Checkbutton(
        controles_base,
        text="Partindo da base",
        variable=partindo_base_var,
    ).pack(side="left", padx=(10, 0))

    ponto_partida_frame = ttk.Frame(coordenadas, style="Card.TFrame")
    ponto_partida_frame.grid(
        row=5, column=0, columnspan=2, sticky="ew", pady=(14, 0)
    )
    ttk.Label(
        ponto_partida_frame, text="Ponto de Partida", style="Field.TLabel"
    ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
    latitude_inicial_entry = ttk.Entry(ponto_partida_frame, width=22)
    latitude_inicial_entry.grid(row=1, column=0, sticky="ew", padx=(0, 6))
    longitude_inicial_entry = ttk.Entry(ponto_partida_frame, width=22)
    longitude_inicial_entry.grid(row=1, column=1, sticky="ew", padx=(6, 0))
    ttk.Label(
        ponto_partida_frame, text="Latitude", style="Field.TLabel"
    ).grid(row=2, column=0, sticky="w", pady=(4, 0))
    ttk.Label(
        ponto_partida_frame, text="Longitude", style="Field.TLabel"
    ).grid(row=2, column=1, sticky="w", padx=(6, 0), pady=(4, 0))
    ponto_partida_frame.columnconfigure(0, weight=1, uniform="coordenada")
    ponto_partida_frame.columnconfigure(1, weight=1, uniform="coordenada")

    ttk.Label(coordenadas, text="Pontos da rota", style="Field.TLabel").grid(
        row=6, column=0, sticky="w", pady=(14, 6)
    )
    pontos_frame = ttk.Frame(coordenadas, style="Card.TFrame")
    pontos_frame.grid(row=7, column=0, columnspan=2, sticky="ew")
    pontos_frame.columnconfigure(0, weight=1)
    pontos_rota: list[tuple[ttk.Frame, ttk.Label, ttk.Entry, ttk.Entry]] = []

    def reordenar_pontos() -> None:
        for indice, ponto in enumerate(pontos_rota, start=1):
            ponto[0].grid(row=indice - 1, column=0, sticky="ew", pady=3)
            ponto[1].configure(text=f"Ponto {indice}")

    def remover_ponto(
        ponto: tuple[ttk.Frame, ttk.Label, ttk.Entry, ttk.Entry],
    ) -> None:
        ponto[0].destroy()
        pontos_rota.remove(ponto)
        reordenar_pontos()

    def adicionar_ponto() -> None:
        indice = len(pontos_rota) + 1
        linha = ttk.Frame(pontos_frame, style="Card.TFrame")
        rotulo_ponto = ttk.Label(
            linha, text=f"Ponto {indice}", style="Field.TLabel"
        )
        rotulo_ponto.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        latitude_ponto = ttk.Entry(linha, width=22)
        latitude_ponto.grid(row=1, column=0, sticky="ew", padx=(0, 6))
        longitude_ponto = ttk.Entry(linha, width=22)
        longitude_ponto.grid(row=1, column=1, sticky="ew", padx=(6, 0))
        ttk.Label(linha, text="Latitude", style="Field.TLabel").grid(
            row=2, column=0, sticky="w", pady=(4, 0)
        )
        ttk.Label(linha, text="Longitude", style="Field.TLabel").grid(
            row=2, column=1, sticky="w", padx=(6, 0), pady=(4, 0)
        )
        ponto = (linha, rotulo_ponto, latitude_ponto, longitude_ponto)
        ttk.Button(
            linha,
            text="Remover",
            style="Clear.TButton",
            command=lambda item=ponto: remover_ponto(item),
        ).grid(row=1, column=2, sticky="ns", padx=(12, 0))
        linha.columnconfigure(0, weight=1, uniform="coordenada_ponto")
        linha.columnconfigure(1, weight=1, uniform="coordenada_ponto")
        pontos_rota.append(ponto)
        reordenar_pontos()

    ttk.Button(
        coordenadas,
        text="Adicionar ponto",
        style="Clear.TButton",
        command=adicionar_ponto,
    ).grid(row=8, column=0, sticky="w", pady=(8, 0))
    adicionar_ponto()
    coordenadas.columnconfigure(0, weight=1)
    coordenadas.columnconfigure(1, weight=1)

    def atualizar_origem(*trace_info: object) -> None:
        del trace_info
        if partindo_base_var.get():
            empreiteira_combo.configure(state="readonly")
            latitude_inicial_entry.configure(state="disabled")
            longitude_inicial_entry.configure(state="disabled")
        else:
            empreiteira_combo.configure(state="disabled")
            latitude_inicial_entry.configure(state="normal")
            longitude_inicial_entry.configure(state="normal")

    partindo_base_var.trace_add("write", atualizar_origem)

    def cadastrar_base() -> None:
        dialogo = tk.Toplevel(root)
        dialogo.title("Cadastrar base da empreiteira")
        dialogo.transient(root)
        dialogo.grab_set()
        dialogo.resizable(False, False)
        painel = ttk.Frame(dialogo, padding=20)
        painel.pack(fill="both", expand=True)

        ttk.Label(painel, text="Nome da empreiteira", style="Field.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        nome_entry = ttk.Entry(painel, width=34)
        nome_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        ttk.Label(painel, text="Latitude da base", style="Field.TLabel").grid(
            row=2, column=0, sticky="w", pady=(0, 6)
        )
        ttk.Label(painel, text="Longitude da base", style="Field.TLabel").grid(
            row=2, column=1, sticky="w", padx=(10, 0), pady=(0, 6)
        )
        latitude_base_entry = ttk.Entry(painel)
        latitude_base_entry.grid(row=3, column=0, sticky="ew")
        longitude_base_entry = ttk.Entry(painel)
        longitude_base_entry.grid(row=3, column=1, sticky="ew", padx=(10, 0))

        def salvar_base() -> None:
            nome = nome_entry.get().strip()
            if not nome:
                messagebox.showerror(
                    "Dados inválidos",
                    "Informe o nome da empreiteira.",
                    parent=dialogo,
                )
                return
            try:
                latitude = ler_coordenada(
                    latitude_base_entry.get(), "latitude", -90, 90
                )
                longitude = ler_coordenada(
                    longitude_base_entry.get(), "longitude", -180, 180
                )
            except ValueError as erro:
                messagebox.showerror("Coordenada inválida", str(erro), parent=dialogo)
                return

            bases_atualizadas = {
                **bases,
                nome: {"latitude": latitude, "longitude": longitude},
            }
            try:
                salvar_bases(bases_atualizadas)
            except OSError as erro:
                messagebox.showerror(
                    "Erro ao salvar",
                    f"Não foi possível salvar a coordenada da base:\n{erro}",
                    parent=dialogo,
                )
                return
            bases.update(bases_atualizadas)
            empreiteira_combo.configure(values=tuple(sorted(bases)))
            empreiteira_combo.set(nome)
            dialogo.destroy()

        ttk.Button(
            painel,
            text="Salvar base",
            style="Calculate.TButton",
            command=salvar_base,
        ).grid(row=4, column=0, sticky="w", pady=(16, 0))
        painel.columnconfigure(0, weight=1)
        painel.columnconfigure(1, weight=1)
        nome_entry.focus_set()

    cadastrar_base_button.configure(command=cadastrar_base)

    result = ttk.Frame(container, style="Card.TFrame", padding=22)
    result.pack(fill="x", pady=(18, 0))
    ttk.Label(result, text="Resultado", style="Section.TLabel").grid(
        row=0, column=0, columnspan=4, sticky="w", pady=(0, 16)
    )

    result_values: dict[str, ttk.Label] = {}
    result_columns = (
        (
            ("hours", "Horas Trabalhadas"),
            ("factor", "Fator da Equipe"),
            ("hh", "Valor de H.H"),
        ),
        (
            ("distance", "Distância ida e volta (km)"),
            ("km_value", "Valor KM linear"),
            ("total", "Total (H.H + KM linear)"),
        ),
    )
    for column, items in enumerate(result_columns):
        result.columnconfigure(column, weight=1)
        for row, (key, title) in enumerate(items, start=1):
            item = ttk.Frame(result, style="Card.TFrame")
            item.grid(
                row=row,
                column=column,
                sticky="ew",
                padx=(0 if column == 0 else 14, 0),
                pady=(0 if row == 1 else 14, 0),
            )
            ttk.Label(item, text=title, style="ResultTitle.TLabel").pack(
                anchor="w"
            )
            value_label = ttk.Label(item, text="—", style="ResultValue.TLabel")
            value_label.pack(anchor="w", pady=(5, 0))
            result_values[key] = value_label

    def calcular(parent: tk.Misc = root) -> float | None:
        acionamento = acionamento_entry.get().strip()
        termino_real = termino_entry.get().strip()
        try:
            datetime.strptime(acionamento, "%H:%M")
            datetime.strptime(termino_real, "%H:%M")
            elementos = int(elementos_combo.get())
            veiculo = veiculo_combo.get()
            if veiculo not in FATORES_KM_VEICULO:
                raise ValueError("Selecione o tipo de veículo.")
            tempo, fator, hh = calcular_hh(
                acionamento,
                termino_real,
                descontar_almoco_var.get(),
                elementos,
            )
            if partindo_base_var.get():
                empreiteira = empreiteira_combo.get()
                if empreiteira not in bases:
                    raise ValueError("Selecione uma empreiteira com base cadastrada.")
                latitude_inicial = bases[empreiteira]["latitude"]
                longitude_inicial = bases[empreiteira]["longitude"]
            else:
                latitude_inicial = ler_coordenada(
                    latitude_inicial_entry.get(), "latitude inicial", -90, 90
                )
                longitude_inicial = ler_coordenada(
                    longitude_inicial_entry.get(), "longitude inicial", -180, 180
                )
            pontos = [
                (
                    ler_coordenada(
                        ponto[2].get(), f"latitude do ponto {indice}", -90, 90
                    ),
                    ler_coordenada(
                        ponto[3].get(),
                        f"longitude do ponto {indice}",
                        -180,
                        180,
                    ),
                )
                for indice, ponto in enumerate(pontos_rota, start=1)
            ]
            km_ida_e_volta, valor_km = calcular_km_rota(
                (latitude_inicial, longitude_inicial),
                pontos,
                veiculo,
            )
        except ValueError as erro:
            messagebox.showerror(
                "Dados inválidos",
                "Confira os horários, as coordenadas e a empreiteira selecionada. "
                f"As coordenadas devem estar em graus decimais.\n\nDetalhe: {erro}",
                parent=parent,
            )
            return None

        horas_trabalhadas = tempo.total_seconds() / 3600
        total = round(hh, 2) + round(valor_km, 4)
        result_values["hours"].configure(text=formatar_decimal(horas_trabalhadas))
        result_values["factor"].configure(text=f"{fator:.4f}".replace(".", ","))
        result_values["hh"].configure(text=formatar_decimal(hh))
        result_values["distance"].configure(
            text=f"{km_ida_e_volta:.3f}".replace(".", ",")
        )
        result_values["km_value"].configure(text=f"{valor_km:.4f}".replace(".", ","))
        result_values["total"].configure(text=f"{total:.4f}".replace(".", ","))
        return total

    def limpar() -> None:
        acionamento_entry.delete(0, tk.END)
        termino_entry.delete(0, tk.END)
        for campo_nota in campos_nsri_ns_mc:
            campo_nota.delete(0, tk.END)
        arquivos_anexados.clear()
        for rotulo in rotulos_anexos.values():
            rotulo.configure(text="Nenhum arquivo selecionado")
        elementos_combo.set("1")
        veiculo_combo.set("")
        descontar_almoco_var.set(True)
        partindo_base_var.set(False)
        empreiteira_combo.set("")
        for entry in (latitude_inicial_entry, longitude_inicial_entry):
            entry.configure(state="normal")
            entry.delete(0, tk.END)
        for ponto in tuple(pontos_rota):
            remover_ponto(ponto)
        adicionar_ponto()
        for value_label in result_values.values():
            value_label.configure(text="—")
        acionamento_entry.focus_set()

    def abrir_cadastro_perfil_ccsm(
        parent: tk.Toplevel,
        perfil_combo: ttk.Combobox,
    ) -> None:
        cadastro = tk.Toplevel(parent)
        cadastro.title("Cadastrar empresa e responsáveis")
        cadastro.transient(parent)
        cadastro.grab_set()
        cadastro.resizable(False, False)
        painel = ttk.Frame(cadastro, padding=20)
        painel.pack(fill="both", expand=True)

        campos: list[tuple[str, str]] = [
            ("Empresa", "empresa"),
            ("Responsável", "responsavel"),
            ("Técnico Supervisor", "supervisor"),
        ]
        entradas: dict[str, ttk.Entry] = {}
        for linha, (rotulo, chave) in enumerate(campos):
            ttk.Label(painel, text=rotulo, style="Field.TLabel").grid(
                row=linha * 2, column=0, sticky="w", pady=(0, 5)
            )
            entrada = ttk.Entry(painel, width=48)
            entrada.grid(row=linha * 2 + 1, column=0, sticky="ew", pady=(0, 12))
            entradas[chave] = entrada

        def salvar_perfil() -> None:
            perfil: PerfilCCSM = {
                "empresa": entradas["empresa"].get().strip(),
                "responsavel": entradas["responsavel"].get().strip(),
                "supervisor": entradas["supervisor"].get().strip(),
            }
            if any(not valor for valor in perfil.values()):
                messagebox.showerror(
                    "Dados incompletos",
                    "Preencha a empresa, o responsável técnico e o supervisor.",
                    parent=cadastro,
                )
                return
            nome = perfil["empresa"]
            if nome in perfis_ccsm and not messagebox.askyesno(
                "Atualizar cadastro",
                f"Já existe um cadastro para {nome}. Deseja substituí-lo?",
                parent=cadastro,
            ):
                return
            atualizados = {**perfis_ccsm, nome: perfil}
            try:
                salvar_perfis_ccsm(atualizados)
            except OSError as erro:
                messagebox.showerror(
                    "Erro ao salvar",
                    f"Não foi possível salvar o perfil CCSM:\n{erro}",
                    parent=cadastro,
                )
                return
            perfis_ccsm.update(atualizados)
            perfil_combo.configure(values=tuple(sorted(perfis_ccsm)))
            perfil_combo.set(nome)
            cadastro.destroy()

        ttk.Button(
            painel,
            text="Salvar cadastro",
            style="Calculate.TButton",
            command=salvar_perfil,
        ).grid(row=len(campos) * 2, column=0, sticky="w")
        painel.columnconfigure(0, weight=1)
        entradas["empresa"].focus_set()

    def abrir_dialogo_gerar_ccsm() -> None:
        dialogo = tk.Toplevel(root)
        dialogo.title("Gerar CCSM")
        dialogo.transient(root)
        dialogo.grab_set()
        dialogo.resizable(False, False)
        painel = ttk.Frame(dialogo, padding=20)
        painel.pack(fill="both", expand=True)

        ttk.Label(painel, text="Empresa e responsáveis", style="Section.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Label(painel, text="Cadastro local", style="Field.TLabel").grid(
            row=1, column=0, sticky="w", pady=(0, 5)
        )
        perfil_combo = ttk.Combobox(
            painel,
            values=tuple(sorted(perfis_ccsm)),
            state="readonly",
            width=38,
        )
        perfil_combo.grid(row=2, column=0, sticky="ew")
        ttk.Button(
            painel,
            text="Cadastrar empresa",
            style="Clear.TButton",
            command=lambda: abrir_cadastro_perfil_ccsm(dialogo, perfil_combo),
        ).grid(row=2, column=1, padx=(8, 0))
        if perfis_ccsm:
            perfil_combo.current(0)

        ttk.Label(painel, text="Destinatários", style="Field.TLabel").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(16, 5)
        )
        destinatarios_vars = {
            nome: tk.BooleanVar(value=False) for nome in ("CEBE", "CEBH", "CESL")
        }
        for coluna, (nome, variavel) in enumerate(destinatarios_vars.items()):
            ttk.Checkbutton(
                painel,
                text=nome,
                variable=variavel,
            ).grid(row=4, column=coluna, sticky="w")

        ttk.Label(painel, text="Tipo de vistoria", style="Field.TLabel").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(12, 5)
        )
        tipo_vistoria_var = tk.StringVar(value="")
        for coluna, (valor, texto) in enumerate(
            (("1", "1ª vistoria"), ("2", "2ª vistoria"), ("3", "3ª vistoria"))
        ):
            ttk.Radiobutton(
                painel,
                text=texto,
                value=valor,
                variable=tipo_vistoria_var,
            ).grid(row=6, column=coluna, sticky="w")

        ttk.Label(painel, text="Data de execução (DD/MM/AAAA)", style="Field.TLabel").grid(
            row=7, column=0, sticky="w", pady=(12, 5)
        )
        data_entry = ttk.Entry(painel, width=18)
        data_entry.grid(row=8, column=0, sticky="w")
        data_entry.insert(0, datetime.now().strftime("%d/%m/%Y"))

        ttk.Label(painel, text="Alteração de US", style="Field.TLabel").grid(
            row=7, column=1, sticky="w", padx=(12, 0), pady=(12, 5)
        )
        alteracao_us_var = tk.StringVar(value="Não")
        ttk.Radiobutton(
            painel, text="Sim", value="Sim", variable=alteracao_us_var
        ).grid(row=8, column=1, sticky="w", padx=(12, 0))
        ttk.Radiobutton(
            painel, text="Não", value="Não", variable=alteracao_us_var
        ).grid(row=8, column=2, sticky="w")

        ttk.Label(painel, text="Observação (opcional)", style="Field.TLabel").grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(12, 5)
        )
        observacao_text = tk.Text(painel, width=58, height=3, wrap="word")
        observacao_text.grid(row=10, column=0, columnspan=3, sticky="ew")

        status_label = ttk.Label(painel, text="", style="Field.TLabel")
        status_label.grid(row=11, column=0, columnspan=3, sticky="w", pady=(8, 0))

        def confirmar_geracao() -> None:
            perfil_nome = perfil_combo.get()
            perfil = perfis_ccsm.get(perfil_nome)
            if perfil is None:
                messagebox.showerror(
                    "Cadastro necessário",
                    "Selecione ou cadastre uma empresa e seus responsáveis.",
                    parent=dialogo,
                )
                return
            destinatarios = [
                nome for nome, variavel in destinatarios_vars.items() if variavel.get()
            ]
            if not destinatarios:
                messagebox.showerror(
                    "Destinatário necessário",
                    "Selecione ao menos um destinatário.",
                    parent=dialogo,
                )
                return
            tipo_vistoria = tipo_vistoria_var.get()
            if tipo_vistoria not in {"1", "2", "3"}:
                messagebox.showerror(
                    "Vistoria necessária",
                    "Selecione o tipo de vistoria.",
                    parent=dialogo,
                )
                return
            nsmc = campos_nsri_ns_mc[1].get().strip()
            if not nsmc:
                messagebox.showerror(
                    "NSMC necessário",
                    "Preencha o campo NSMC na tela principal.",
                    parent=dialogo,
                )
                return
            try:
                data_execucao = datetime.strptime(
                    data_entry.get().strip(), "%d/%m/%Y"
                ).strftime("%d/%m/%Y")
            except ValueError:
                messagebox.showerror(
                    "Data inválida",
                    "Informe a data de execução no formato DD/MM/AAAA.",
                    parent=dialogo,
                )
                return

            total = calcular(dialogo)
            if total is None:
                return
            try:
                primeiro_ponto = pontos_rota[0]
                latitude = ler_coordenada(
                    primeiro_ponto[2].get(), "latitude do primeiro ponto", -90, 90
                )
                longitude = ler_coordenada(
                    primeiro_ponto[3].get(),
                    "longitude do primeiro ponto",
                    -180,
                    180,
                )
            except (IndexError, ValueError) as erro:
                messagebox.showerror(
                    "Primeiro ponto inválido",
                    str(erro),
                    parent=dialogo,
                )
                return

            observacao = observacao_text.get("1.0", tk.END).strip()
            if len(observacao) > 180:
                messagebox.showerror(
                    "Observação muito longa",
                    "A observação deve ter no máximo 180 caracteres.",
                    parent=dialogo,
                )
                return
            caminho_saida = filedialog.asksaveasfilename(
                parent=dialogo,
                title="Salvar CCSM",
                defaultextension=".png",
                filetypes=(("Imagem PNG", "*.png"),),
                initialfile=f"CCSM_{nsmc}.png",
            )
            if not caminho_saida:
                return

            dados: DadosCCSM = {
                "destinatarios": destinatarios,
                "perfil": perfil,
                "nsmc": nsmc,
                "tipo_vistoria": tipo_vistoria,
                "municipio": "",
                "data_execucao": data_execucao,
                "alteracao_us": alteracao_us_var.get(),
                "total_us": f"{total:.3f}".replace(".", ","),
                "observacao": observacao,
            }
            gerar_botao.configure(state="disabled")
            dialogo.protocol("WM_DELETE_WINDOW", lambda: None)
            status_label.configure(
                text="Consultando o município do primeiro ponto online..."
            )
            dialogo.update_idletasks()

            def consultar_municipio() -> None:
                try:
                    municipio = geocodificar_municipio(latitude, longitude)
                except RuntimeError as erro:
                    root.after(
                        0,
                        lambda detalhe=str(erro): finalizar_geracao(erro=detalhe),
                    )
                    return
                root.after(
                    0,
                    lambda nome=municipio: finalizar_geracao(municipio=nome),
                )

            def finalizar_geracao(
                municipio: str | None = None,
                erro: str | None = None,
            ) -> None:
                gerar_botao.configure(state="normal")
                if erro:
                    dialogo.protocol("WM_DELETE_WINDOW", dialogo.destroy)
                    status_label.configure(text="")
                    messagebox.showerror(
                        "Erro na consulta online",
                        erro,
                        parent=dialogo,
                    )
                    return
                if municipio is None:
                    dialogo.protocol("WM_DELETE_WINDOW", dialogo.destroy)
                    status_label.configure(text="")
                    messagebox.showerror(
                        "Erro na consulta online",
                        "A consulta não retornou um município.",
                        parent=dialogo,
                    )
                    return
                dados["municipio"] = municipio
                try:
                    gerar_imagem_ccsm(caminho_saida, dados)
                except (OSError, ValueError, KeyError, IndexError, RuntimeError) as erro_imagem:
                    dialogo.protocol("WM_DELETE_WINDOW", dialogo.destroy)
                    status_label.configure(text="")
                    messagebox.showerror(
                        "Erro ao gerar imagem CCSM",
                        f"Não foi possível gerar a imagem:\n{erro_imagem}",
                        parent=dialogo,
                    )
                    return
                status_label.configure(text="Imagem CCSM gerada com sucesso.")
                messagebox.showinfo(
                    "Imagem CCSM gerada",
                    f"Arquivo salvo em:\n{caminho_saida}\n\n"
                    f"Município consultado: {municipio}\n"
                    "Geocodificação: © OpenStreetMap contributors",
                    parent=dialogo,
                )
                dialogo.destroy()

            threading.Thread(target=consultar_municipio, daemon=True).start()

        gerar_botao = ttk.Button(
            painel,
            text="Gerar imagem CCSM",
            style="Calculate.TButton",
            command=confirmar_geracao,
        )
        gerar_botao.grid(row=12, column=0, sticky="w", pady=(12, 0))
        painel.columnconfigure(0, weight=1)

    buttons = ttk.Frame(container)
    buttons.pack(fill="x", pady=(18, 0))
    ttk.Button(
        buttons,
        text="Calcular H.H e KM",
        style="Calculate.TButton",
        command=calcular,
        width=18,
    ).grid(row=0, column=0, sticky="w")
    ttk.Button(
        buttons,
        text="Limpar",
        style="Clear.TButton",
        command=limpar,
        width=12,
    ).grid(row=0, column=1, sticky="w", padx=(10, 0))

    ttk.Button(
        buttons,
        text="Gerar CCSM",
        style="Calculate.TButton",
        command=abrir_dialogo_gerar_ccsm,
        width=14,
    ).grid(row=0, column=2, sticky="w", padx=(10, 0))

    for coluna, tipo in enumerate(("Anexo ACR", "Anexo CAP", "Anexo Croqui"), start=3):
        ttk.Button(
            buttons,
            text=tipo,
            style="Clear.TButton",
            command=lambda anexo=tipo: selecionar_anexo(anexo),
            width=12,
        ).grid(row=0, column=coluna, sticky="w", padx=(10, 0))
        rotulo = ttk.Label(
            buttons,
            text="Nenhum arquivo selecionado",
            style="ResultTitle.TLabel",
            wraplength=140,
        )
        rotulo.grid(row=1, column=coluna, sticky="w", padx=(10, 0), pady=(6, 0))
        rotulos_anexos[tipo] = rotulo

    ttk.Label(
        container,
        text="© 2026 | Desenvolvido por Eliezer Temperani - C058541",
        style="Subtitle.TLabel",
        wraplength=600,
    ).pack(anchor="w", pady=(18, 0))

    root.mainloop()


if __name__ == "__main__":
    criar_janela()
