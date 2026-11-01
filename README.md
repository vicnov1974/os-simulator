# Simulador de Sistema Operacional Modular

Projeto em 5 etapas incrementais. Este repositório contém a **Etapa 1: Sistema de Arquivos e Diagnóstico de Disco**.

- **Linguagem:** Python 3.9+ (somente biblioteca padrão, sem dependências)
- **Interfaces:** shell interativo (REPL), scripts de comandos e **interface gráfica (Tkinter) estilo Windows Explorer**

## Como executar

```bash
git clone <URL-DO-SEU-REPOSITORIO>
cd os-simulator

python main.py                                   # shell interativo (disco de 16 x 4 KB, indexado)
python main.py --alloc contiguous --block-size 1KB --blocks 32
python main.py --script examples/01_enunciado_indexada.txt   # roda um script de demonstração
```

Testes (opcional): `pip install -r requirements-dev.txt && pytest` (os testes da GUI são pulados se não houver display)

## Interface gráfica

```bash
python gui.py                                    # disco de 64 x 4 KB, alocação indexada
python gui.py --alloc contiguous --block-size 1KB --blocks 128
```

![Interface gráfica](docs/gui.png)

- **Esquerda:** árvore de pastas. **Direita:** conteúdo da pasta (tipo, tamanho, blocos ocupados, fragmentação interna).
- **Barra de endereço** (caminhos absolutos e relativos), botões voltar/avançar/subir.
- **Botão direito:** abrir, renomear (F2), excluir (Del), propriedades, nova pasta/arquivo. Duplo clique abre pasta ou editor de texto.
- **Mapa do disco:** um quadrado por bloco, uma cor por arquivo. Blocos parcialmente preenchidos mostram em rosa a parte desperdiçada (fragmentação interna). Passe o mouse para ver o detalhe do bloco; clique para selecionar o arquivo dono.
- **Painel de diagnóstico:** fragmentação interna/externa e faixa vermelha **COMPACTAÇÃO NECESSÁRIA** (com botão para compactar).
- **Alocação** trocável na barra superior (migra os arquivos); *Disco > Formatar* recria o disco com outro tamanho de bloco.

> Tkinter vem com o Python no Windows e no macOS. No Linux: `sudo apt install python3-tk`.

## O que a Etapa 1 implementa

| Requisito | Implementação |
|---|---|
| Hierarquia em árvore | `osim/filesystem.py` - nós `Node` (diretório/arquivo), diretório corrente, caminhos absolutos (`/a/b`) e relativos (`a/b`, `./a`, `../a`) |
| CRUD | `mkdir`, `create`, `cat`, `write`, `append`, `resize`, `mv`, `cp`, `rm`, `rmdir` |
| Camada física em blocos | `osim/disk.py` - disco com blocos de tamanho fixo (configurável: 512 B, 1 KB, 4 KB...) |
| Estratégias de alocação | `osim/allocation.py` - **as três**: Contígua (first-fit), Encadeada (FAT) e Indexada (i-node), via *Strategy Pattern* |
| Mapa gráfico do disco | `fs_status` - grade de blocos com cores/símbolos por arquivo + lista bloco a bloco com `--details` |
| Fragmentação interna | por arquivo e total (bytes e % de desperdício no último bloco) |
| Fragmentação externa | regiões livres, maior região contígua, bytes "presos" fora da maior região |
| Necessidade de compactação | indicação explícita SIM/NÃO com justificativa; comando `compact` resolve |

### Extras

- `set_alloc <estratégia>` migra o disco inteiro para outra estratégia sem perder dados.
- `compact` reorganiza o disco (atômico: em caso de erro, restaura o estado anterior).
- Atualizações que não cabem fazem *rollback* e preservam o arquivo original.
- Limite de i-node realista: `tamanho_do_bloco / 4` ponteiros por arquivo.

## Modelo de disco

- Cada **diretório** ocupa 1 bloco de metadados (`DIR_RAIZ`/`DIR`; `INODE_RAIZ`/`INODE_DIR` no modo indexado).
- **Indexada:** cada arquivo ocupa 1 bloco de i-node (`INODE_ARQ`) + blocos de dados.
- **Encadeada:** só blocos de dados; cada um indica o próximo (`-> próximo: NN`, `-> FIM`).
- **Contígua:** só blocos de dados, sempre consecutivos (a primeira região livre que couber).
- Arquivos criados com `--size` são preenchidos com zeros; o `cat` os exibe como conteúdo binário.

## Comandos

```
pwd | cd <dir> | ls [-l] [caminho] | tree [caminho] | stat <caminho>
mkdir [-p] <dir> | create <arq> [--size 10KB] [--content "texto"]
cat <arq> | write <arq> <texto> | append <arq> <texto> | resize <arq> <tamanho>
mv <origem> <destino> | cp <origem> <destino> | rm [-r] <caminho> | rmdir <dir>
fs_status [--details] | compact | set_alloc <contiguous|linked|indexed>
format [--block-size 4KB] [--blocks 16] [--alloc indexed]
help | exit
```

## Exemplo (alocação indexada, saída real do simulador)

```
/> mkdir /sistema
/> mkdir /sistema/logs
/> create /sistema/logs/app.log --size 10KB
/> tree
/
└── sistema/ [DIR]
    └── logs/ [DIR]
        └── app.log (10 KB) [ARQ]

/> fs_status --details
Tamanho do Bloco de Disco: 4 KB | Total de Blocos: 16 (64 KB)
Estratégia de Alocação   : Indexada (i-node)

[MAPA GRÁFICO DO DISCO]
00 | [#][#][#][@][A][A](A) .  .  .  .  .  .  .  .  .
Legenda: [#] diretório | [@] i-node | [X] bloco cheio | (X) bloco parcial = fragmentação interna | . livre

[MAPA DE ALOCAÇÃO DO DISCO - Indexada (i-node)]
Bloco 00 [INODE_RAIZ]   : Diretório '/'
Bloco 01 [INODE_DIR]    : Diretório '/sistema'
Bloco 02 [INODE_DIR]    : Diretório '/sistema/logs'
Bloco 03 [INODE_ARQ]    : Arquivo '/sistema/logs/app.log' (índice -> blocos: 04, 05, 06)
Bloco 04 [DADO]         : '/sistema/logs/app.log' (Bloco 1 - 4096 bytes ocupados)
Bloco 05 [DADO]         : '/sistema/logs/app.log' (Bloco 2 - 4096 bytes ocupados)
Bloco 06 [DADO]         : '/sistema/logs/app.log' (Bloco 3 - 2048 bytes ocupados)
Bloco 07..15            : [LIVRE]

--- RELATÓRIO DE FRAGMENTAÇÃO (GLOBAL) ---
Fragmentação Interna             : 2048 bytes (1 arquivo(s) afetado(s); 16.7% do espaço alocado a dados)
Fragmentação Externa             : 0 bytes (não se aplica: alocação não-contígua)
Necessidade de Compactação       : NÃO (alocação não-contígua não exige blocos adjacentes)
```

## Exemplo (alocação contígua com fragmentação externa)

Rode `python main.py --script examples/02_contigua_fragmentacao.txt`. Após remover um arquivo do meio, o relatório mostra:

```
[MAPA GRÁFICO DO DISCO]
00 | [#][A][A][A] .  .  . [B][B][C](C) .  .  .  .  .

Blocos Livres                    : 8 (8192 bytes) em 2 região(ões) [04..06], [11..15]
Maior Região Livre Contígua      : 5 bloco(s) (5120 bytes)
Fragmentação Externa             : 3072 bytes (3 bloco(s) livres dispersos fora da maior região)
Necessidade de Compactação       : SIM - o espaço livre está dividido; arquivos maiores que 5 bloco(s) não podem ser alocados. Use o comando 'compact'.

/> create /grande.dat --size 6KB
Erro: fragmentação externa: há 8 blocos livres, mas a maior região contígua tem só 5 (necessário: 6). Execute 'compact' ...
/> compact
/> create /grande.dat --size 6KB       # agora cabe
```

## Estrutura do repositório

```
os-simulator/
├── main.py                 # ponto de entrada (CLI / REPL / scripts)
├── gui.py                  # ponto de entrada da interface gráfica
├── docs/gui.png
├── osim/
│   ├── disk.py             # camada física: blocos, donos, regiões livres
│   ├── allocation.py       # Contígua / Encadeada (FAT) / Indexada (i-node)
│   ├── filesystem.py       # árvore, caminhos, CRUD, compactação, migração
│   ├── report.py           # mapa gráfico, fragmentação, compactação
│   ├── shell.py            # interpretador de comandos
│   ├── gui.py              # interface gráfica (Tkinter)
│   └── utils.py            # parse/format de tamanhos
├── examples/               # scripts de demonstração
└── tests/                  # test_filesystem.py, test_gui.py
```

## Próximas etapas

A separação em módulos (`Disk` -> `AllocationStrategy` -> `FileSystem` -> `Shell`) foi pensada para que as etapas seguintes (processos, memória virtual e segurança) se encaixem como novas camadas sem reescrever o sistema de arquivos.
