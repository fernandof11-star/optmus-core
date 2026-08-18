"""Camadas de memoria do Optmus Core e a persistencia que as sustenta.

    trabalho     conversa atual, RAM, TTL 30min          working.py
    episodica    o que aconteceu, quando, com quem       episodic.py
    semantica    fatos sobre o usuario e o mundo dele    semantic.py
    procedural   rotinas derivadas dos episodios         procedural.py

    perfil vivo  preferencias e projetos, em perfil.md   profile.py
    consolidador o "sono": digere o dia de madrugada     consolidator.py
    fachada      quem usa memoria fala com ela           system.py

Este ``__init__`` exporta so o que e folha de dependencia. ``MemorySystem`` e
``Consolidator`` moram em modulos que dependem de ``core.bus``, e ``core.bus``
depende de ``memory.store`` - reexporta-los aqui fecharia um ciclo de import.
Importe-os do modulo direto: ``from memory.system import MemorySystem``.
"""

from memory.scoring import MemoryHit
from memory.store import Store, StoreError
from memory.working import WorkingMemory

__all__ = ["MemoryHit", "Store", "StoreError", "WorkingMemory"]
