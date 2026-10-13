---
name: skill-vetting
description: "Checklist e protocolo de auditoria metódica para avaliar e homologar skills externas no catálogo do sistema. Verificação de conformidade de licença (MIT, Apache 2.0), saúde e manutenção do repositório de origem, validação estrita de frontmatter YAML com Pydantic, varredura de segurança contra instruções executáveis ou chamadas de rede, e quarentena preventiva até aprovação humana formal. External skill security audit, license verification, YAML frontmatter validation, and safe quarantine protocols."
director: lyra
source: "internal"
license: MIT
status: active
version: "1.0.0"
token_budget: 750
---

# Auditoria e Homologação de Skills Externas (Skill Vetting)

Protocolo de segurança, compliance e governança técnica para avaliar, auditar e classificar skills importadas de repositórios externos antes da disponibilização no catálogo de agentes.

## Quando Usar
- Ao incorporar uma nova skill de fontes externas (ex: repositórios GitHub de terceiros, pacotes comunitários).
- Quando o usuário solicitar auditoria de segurança ou verificação de conformidade de uma skill.
- Para decidir se uma skill pode ser ativada (`status: active`) ou deve permanecer isolada (`status: quarantined`).

## Passos
1. **Verificação de Licença de Código Aberto**:
   - Confirmar licença permissiva compatível (MIT, Apache 2.0, BSD). Rejeitar licenças restritivas ou ausência de licença.
2. **Inspeção de Saúde e Manutenção**:
   - Checar data do último commit, histórico de manutenibilidade e proveniência do autor.
3. **Validação Estrita de Frontmatter YAML**:
   - Garantir todos os campos obrigatórios pelo modelo Pydantic: `name`, `description`, `director`, `source`, `license`, `status`, `version`.
   - Garantir que a descrição seja rica e bilíngue (pt-BR e en-US) para permitir roteamento semântico preciso.
4. **Varredura Estática de Comandos e Rede**:
   - Buscar menções a comandos executáveis de shell (`bash`, `sh`, `curl`, `wget`, `npm`, `pip`, `python -m`, etc.).
   - Buscar tentativas de conexão de rede ou submissão remota não autorizada a trackers externos.
   - Se instruções de execução forem identificadas: sanitizar removendo as chamadas OU marcar imediatamente como `status: quarantined`.
5. **Decisão e Quarentena Preventiva**:
   - Qualquer skill com comportamento potencialmente arriscado permanece em quarentena até revisão humana expressa.

## Critérios de Verificação
- Todos os 5 itens do checklist foram inspecionados e documentados.
- Nenhuma instrução de download arbitrário, execução de binários ou acesso não autorizado à rede passa despercebida.
- Skills reprovadas ou suspeitas recebem `status: quarantined`, sendo indexadas no catálogo porém bloqueadas no router.

## Formato de Saída
```markdown
## Relatório de Vetting de Skill Externa

- **Skill**: <nome / slug>
- **Fonte (URL)**: <url exata>
- **Licença**: <MIT / Apache / Outra> [Aprovada / Rejeitada]
- **Frontmatter YAML**: [Válido (Pydantic) / Inválido]

### Itens de Segurança
- [x] Licença de código aberto compatível verificada
- [x] Repositório ativo e proveniência identificada
- [x] Frontmatter YAML validado com schema Pydantic
- [ ] Presença de instruções de rede ou comandos executáveis detectada? [Não / Sim: <detalhes>]

### Decisão Final
- **Status Atribuído**: `active` | `quarantined`
- **Parecer**: <Justificativa técnica objetiva e recomendações>
```
