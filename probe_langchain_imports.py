import importlib
import pkgutil

print('langchain module path:')
import langchain
print(langchain.__file__)

print('\nchat_models package contents:')
import langchain.chat_models as chat_models
print(chat_models.__file__)
print([name for _, name, _ in pkgutil.iter_modules(chat_models.__path__)])

print('\nlangchain submodules containing ollama or groq:')
for finder, name, ispkg in pkgutil.walk_packages(langchain.__path__, prefix='langchain.'):
    if 'ollama' in name.lower() or 'groq' in name.lower() or 'chat' in name.lower():
        print(name, 'pkg' if ispkg else 'module')
