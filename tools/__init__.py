"""Ferramentas do Optmus.

Nucleo burro, ferramentas inteligentes (secao 3.1): o orquestrador so decide
*qual* ferramenta chamar. Toda a logica de dominio mora aqui, o que permite
acrescentar capacidade sem tocar no loop de agente.
"""

from tools.registry import Tool, ToolRegistry, ToolResult

__all__ = ["Tool", "ToolRegistry", "ToolResult"]
