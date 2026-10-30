"""Technology dictionary and exact-match search, like an ATS (resume screening system) does.

TECH_TERMS maps a canonical name (as shown in the report) to every spelling that is
searched. Terms learned by the analysis live in a separate JSON file; all_terms merges
both, and the dictionary in this file always wins. The script never edits this file.
"""
import functools
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path

LEARNED_TERMS_PATH = Path("reports/cache/learned_terms.json")  # relative to the working directory
MAX_TERM_CHARS = 40  # used by terms_to_learn; kept here with the other term constants
SHORT_ALIAS_MAX_LETTERS = 2

# The alias tuple is exhaustive: the canonical name is searched only if it is in the tuple.
# Ambiguous names (REST, Clarity, get_it) are left out on purpose.
TECH_TERMS: dict[str, tuple[str, ...]] = {
    # Flutter / Dart
    "Flutter": ("flutter",),
    "Dart": ("dart",),
    "BLoC": ("bloc", "flutter_bloc", "cubit"),
    "Riverpod": ("riverpod", "flutter_riverpod", "hooks_riverpod"),
    "Provider": ("provider",),
    "MobX": ("mobx",),
    "GetX": ("getx",),
    "get_it": ("get_it", "getit"),
    "Dio": ("dio",),
    "Freezed": ("freezed",),
    "GoRouter": ("go_router", "gorouter", "go router"),
    "Hive": ("hive",),
    "Isar": ("isar",),
    "Drift": ("drift",),
    "Flutter Modular": ("flutter_modular", "flutter modular"),
    # Native mobile
    "Kotlin": ("kotlin",),
    "Kotlin Multiplatform": ("kotlin multiplatform", "kmp"),
    "Swift": ("swift",),
    "Java": ("java",),
    "Objective-C": ("objective-c", "objective c", "objc"),
    "React Native": ("react native", "react-native"),
    "Jetpack Compose": ("jetpack compose",),
    "SwiftUI": ("swiftui",),
    "Android": ("android",),
    "iOS": ("ios",),
    # Firebase
    "Firebase": ("firebase",),
    "Firestore": ("firestore", "cloud firestore"),
    "Firebase Auth": ("firebase auth", "firebase authentication"),
    "Cloud Functions": ("cloud functions", "firebase functions"),
    "Remote Config": ("remote config", "firebase remote config"),
    "FCM": ("fcm", "firebase cloud messaging"),
    "Firebase Analytics": ("firebase analytics",),
    "Firebase App Distribution": ("firebase app distribution",),
    # Backend and languages
    "REST": ("api rest", "apis rest", "rest api", "rest apis", "restful", "api restful", "apis restful"),
    "GraphQL": ("graphql",),
    "gRPC": ("grpc",),
    "WebSocket": ("websocket", "websockets", "web socket"),
    "Node.js": ("node.js", "nodejs", "node js"),
    "Supabase": ("supabase",),
    "PostgreSQL": ("postgresql", "postgres"),
    "SQLite": ("sqlite", "sqflite"),
    "MongoDB": ("mongodb", "mongo"),
    "Go": ("Go", "golang"),
    "Python": ("python",),
    "TypeScript": ("typescript",),
    "JavaScript": ("javascript",),
    "C#": ("c#", "csharp"),
    "C++": ("c++",),
    # Architecture
    "Clean Architecture": ("clean architecture", "arquitetura limpa", "clean arch"),
    "Clean Code": ("clean code", "codigo limpo"),
    "MVVM": ("mvvm",),
    "MVC": ("mvc",),
    "SOLID": ("solid",),
    "DDD": ("ddd", "domain-driven design", "domain driven design"),
    "Monorepo": ("monorepo", "mono repo", "mono-repo"),
    "Micro-frontends": (
        "micro-frontends", "micro frontends", "microfrontends", "microapps", "micro apps", "micro-apps",
    ),
    "Design System": ("design system", "design systems"),
    "Melos": ("melos",),
    # Quality
    "TDD": ("tdd",),
    "BDD": ("bdd",),
    "Testes unitários": (
        "testes unitarios", "teste unitario", "testes de unidade", "unit test", "unit tests", "unit testing",
    ),
    "Testes de widget": ("testes de widget", "widget test", "widget tests", "widget testing"),
    "Testes de integração": (
        "testes de integracao", "teste de integracao", "integration test", "integration tests",
        "integration testing",
    ),
    "Golden tests": ("golden test", "golden tests", "golden files", "testes golden"),
    "Mocktail": ("mocktail",),
    "Mockito": ("mockito",),
    "Patrol": ("patrol",),
    # CI/CD
    "CI/CD": (
        "ci/cd", "ci cd", "integracao continua", "continuous integration", "entrega continua",
        "continuous delivery", "continuous deployment",
    ),
    "GitHub Actions": ("github actions",),
    "GitLab CI": ("gitlab ci", "gitlab-ci"),
    "Azure DevOps": ("azure devops",),
    "Codemagic": ("codemagic",),
    "Bitrise": ("bitrise",),
    "Fastlane": ("fastlane",),
    "Git": ("git",),
    # Observability
    "Sentry": ("sentry",),
    "Crashlytics": ("crashlytics", "firebase crashlytics"),
    "Datadog": ("datadog",),
    "New Relic": ("new relic", "newrelic"),
    "PostHog": ("posthog",),
    "Clarity": ("microsoft clarity",),
    "Mixpanel": ("mixpanel",),
    # Cloud
    "AWS": ("aws", "amazon web services"),
    "GCP": ("gcp", "google cloud", "google cloud platform"),
    "Azure": ("azure", "microsoft azure"),
    "Docker": ("docker",),
    "Kubernetes": ("kubernetes", "k8s"),
    "Cloudflare": ("cloudflare",),
    # Agile and tools
    "Scrum": ("scrum",),
    "Kanban": ("kanban",),
    "Jira": ("jira",),
    "Confluence": ("confluence",),
    "Figma": ("figma",),
    # Publishing
    "Google Play": ("google play", "play store", "playstore", "google play console"),
    "App Store": ("app store", "appstore", "apple store"),
    "Flavors": ("flavors", "flavours", "build flavors"),
    "Deep Link": ("deep link", "deep links", "deeplink", "deeplinks", "deep linking"),
    "Push Notification": (
        "push notification", "push notifications", "notificacao push", "notificacoes push",
    ),
    "i18n/l10n": ("i18n", "l10n", "internacionalizacao", "internationalization"),
    "Acessibilidade": ("acessibilidade", "accessibility", "a11y"),
    # AI
    "LLM": ("llm", "llms", "large language model", "large language models"),
    "OpenAI": ("openai", "chatgpt", "gpt"),
    "Claude": ("claude", "anthropic"),
    "GitHub Copilot": ("github copilot", "copilot"),
    "Cursor": ("cursor",),
}


def normalize(text: str) -> str:
    """Lowercase, strip accents and collapse whitespace; punctuation and symbols stay."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


def _is_short(alias: str) -> bool:
    return alias.isalpha() and len(alias) <= SHORT_ALIAS_MAX_LETTERS


@functools.lru_cache(maxsize=None)
def _pattern(form: str, short: bool) -> re.Pattern[str]:
    """Whole-word pattern; short aliases are case sensitive and reject '-', '+' and '#' around."""
    escaped = re.escape(form)
    if short:
        return re.compile(rf"(?<![\w-]){escaped}(?![\w+#-])")
    return re.compile(rf"(?<!\w){escaped}(?!\w)")


def load_learned(path: Path) -> dict[str, tuple[str, ...]]:
    """Read the learned terms; a missing or invalid file gives {}."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    learned: dict[str, tuple[str, ...]] = {}
    for name, aliases in raw.items():
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            continue
        learned[name] = tuple(a.strip() for a in aliases if a.strip())
    return learned


def save_learned(path: Path, terms: dict[str, tuple[str, ...]]) -> None:
    """Write the learned terms atomically (temp file in the same folder + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: list(aliases) for name, aliases in terms.items()}
    fd, tmp_name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(payload, tmp, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def all_terms(learned: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """TECH_TERMS plus the learned terms; the dictionary in the code always wins."""
    merged = dict(TECH_TERMS)
    names = {normalize(name) for name in merged}
    owned = {normalize(alias) for aliases in merged.values() for alias in aliases}
    for name in sorted(learned):
        if normalize(name) in names:
            continue
        forms: list[str] = []
        seen: set[str] = set()
        for form in (name, *learned[name]):
            key = normalize(form)
            if not key or key in seen or key in owned:
                continue
            seen.add(key)
            forms.append(form)
        if not forms:
            continue
        merged[name] = tuple(forms)
        names.add(normalize(name))
        owned |= seen
    return merged


def find_technologies(text: str, terms: dict[str, tuple[str, ...]] = TECH_TERMS) -> set[str]:
    """Canonical names with at least one spelling found in the text as a whole word."""
    if not text.strip():
        return set()
    normalized = normalize(text)
    found: set[str] = set()
    for name, aliases in terms.items():
        for alias in aliases:
            if _is_short(alias):
                matched = _pattern(alias, True).search(text)
            else:
                matched = _pattern(normalize(alias), False).search(normalized)
            if matched:
                found.add(name)
                break
    return found
