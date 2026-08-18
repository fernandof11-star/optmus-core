"""Camada de politica: quem pode executar o que, e o que fica registrado.

O kill switch nao mora aqui: ele e o caminho mais quente do sistema e vive no
loop de voz (camada 1 do roteador) e em ``POST /sistema/parar``. Passar por um
modulo de politica para poder dizer "para" seria acrescentar uma dependencia
exatamente no comando que precisa funcionar com tudo o mais quebrado.
"""

from security.audit import AuditLog
from security.policy import Decisao, PolicyEngine, RiskLevel

__all__ = ["AuditLog", "Decisao", "PolicyEngine", "RiskLevel"]
