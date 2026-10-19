"""Recuperação local por assunto, com autorização pela pergunta original."""
from functools import cache
import math
import re
import unicodedata

from .store import ROOT


IGNORED_WORDS = set('''a ao aos as ate com como da das de do dos e em entre esse esta
    este eu ha na nas no nos o os ou para pela pelo por qual quais quando que quem
    sao se sem ser sua seu tem um uma voce posso pode podem preciso quero gostaria
    saber informe explique regra regras regulamento condominio residencial morador
    moradores capitulo capitulos art assunto tudo'''.split())

# Termos de busca, não conteúdo do regulamento. Verbos genéricos como "reservar"
# não são atalhos para áreas de lazer: também se reserva elevador para mudança.
ALIASES = {
    'I': 'sindico administracao residencial locacao remoto remotamente home office omissos',
    'II': 'direito dever cota boleto balancetes',
    'III': 'barulho ruido sossego som',
    'IV': 'nadar natacao',
    'V': 'musculacao brinquedo',
    'VI': 'churrasco poliesportiva',
    'VII': 'visitante encomenda entregador camera tag acesso',
    'VIII': 'animal cachorro cao caes gato pet focinheira coleira',
    'IX': 'mudar icamento',
    'X': 'reformar entulho construcao',
    'XI': 'vaga carro bicicleta estacionamento',
    'XII': 'reciclar descarte residuo',
    'XIII': 'multa advertencia infracao penalidade defesa recurso',
    'XIV': 'alterar alterado alterada alteracao vigencia vigor revisao',
}


def terms(text):
    normalized = ''.join(char for char in unicodedata.normalize('NFD', text.lower())
                         if unicodedata.category(char) != 'Mn')
    words = re.findall(r'[a-z0-9]+', normalized)
    return {word[:-1] if word.endswith('s') and len(word) > 4 else word
            for word in words if len(word) > 2 and word not in IGNORED_WORDS}


@cache
def index():
    document = (ROOT / 'dados' / 'regulamento.md').read_text(encoding='utf-8')
    headings = list(re.finditer(r'^## Capítulo ([IVX]+): (.+)$', document, re.M))
    chapters = {}
    for position, heading in enumerate(headings):
        end = headings[position + 1].start() if position + 1 < len(headings) else len(document)
        body = document[heading.end():end].strip()
        articles = [part.strip() for part in re.split(r'\n(?=\*\*Art\.)', body) if part.strip()]
        chapters[heading[1]] = {
            'heading': heading[0], 'title': terms(heading[2]),
            'aliases': terms(ALIASES.get(heading[1], '')),
            'articles': [(article, terms(article)) for article in articles],
        }
    # Palavras presentes em vários capítulos, como "administração", pesam menos
    # que palavras específicas. Uma única menção genérica no corpo não basta.
    vocabulary = [set().union(*(tokens for _, tokens in chapter['articles'])) for chapter in chapters.values()]
    weights = {token: math.log(1 + len(chapters) / sum(token in tokens for tokens in vocabulary))
               for token in set().union(*vocabulary)}
    return chapters, weights


def candidates(query):
    chapters, weights = index()
    tokens = terms(query)
    scores = {}
    for number, chapter in chapters.items():
        title = len(tokens & chapter['title'])
        alias = len(tokens & chapter['aliases'])
        overlap = max((len(tokens & article) for _, article in chapter['articles']), default=0)
        if not title and not alias and overlap < 2:
            continue
        body = max((sum(weights.get(token, 1) for token in tokens & article)
                    for _, article in chapter['articles']), default=0)
        scores[number] = 10 * title + 4 * alias + min(body, 3)
    if not scores:
        return set()
    best = max(scores.values())
    return {number for number, score in scores.items() if math.isclose(score, best)}


def regulation_excerpt(subject: str, user_text: str):
    """Só devolve um capítulo sustentado tanto pela pergunta quanto pela tool."""
    allowed = candidates(user_text)
    selected = allowed & candidates(subject)
    if len(selected) != 1:
        return {'status': 'especifique_assunto',
                'texto': 'Especifique um assunto do regulamento por vez para eu consultar o trecho correto.'}
    number = selected.pop()
    chapters, weights = index()
    chapter = chapters[number]
    query = terms(user_text) | terms(subject)
    ranked = sorted(enumerate(chapter['articles']), key=lambda item:
                    -sum(weights.get(token, 1) for token in query & item[1][1]))
    chosen = sorted(position for position, _ in ranked[:3])
    excerpt = '\n\n'.join([chapter['heading'], *(chapter['articles'][position][0] for position in chosen)])
    return {'fonte': 'regulamento.md', 'capitulo': number, 'trecho': excerpt}
