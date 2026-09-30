
## Task: Draft acceptance criteria for the task detail panel (2026-09-30 13:54:23)
---
**SOUL.md Update** | **Task: `verify-aura`**
**Status:** ✅ **Accepted** | **Priority:** Medium
**Last Modified:** 2026-08-21 | **Context Budget:** 1.8/2.0k tokens

---

### **1. Task Breakdown & Acceptance Criteria**
**Objective:** Draft **formal acceptance criteria** for the **Task Detail Panel** (TDP) to ensure alignment with **Aura’s Product & UX Standards**.

#### **Core Requirements (Non-Negotiable)**
| **Criteria**                          | **Validation Rule**                                                                 | **Pass/Fail** |
|---------------------------------------|-----------------------------------------------------------------------------------|---------------|
| **PRD Alignment**                     | Must reference an **approved PRD** (e.g., `PRD-TDP-001`) with signed-off user stories. | ❌ Fail if missing. |
| **Design System Compliance**          | Components must use **Shadcn/UI** (e.g., `Card`, `Tabs`, `Badge`) with **Aura’s design tokens**. | ❌ Fail if custom CSS overrides core tokens. |
| **UX Heuristics (Nielsen)**           | Must pass **all 10 heuristics** (e.g., visibility of system status, error prevention). | ❌ Fail if >1 heuristic violated. |
| **Adversarial UX Test**               | Panel must survive **5+ edge cases** (e.g., empty state, rapid clicks, invalid inputs). | ❌ Fail if crashes or UX breaks. |
| **Visual Fidelity**                   | Mockup must match **Claude Design System** (e.g., typography, spacing, micro-interactions). | ❌ Fail if "functional but ugly." |
| **Accessibility (a11y)**              | WCAG 2.1 AA compliance (e.g., ARIA labels, contrast ratios, keyboard nav).          | ❌ Fail if automated tools flag errors. |

---

### **2. Proposed User Stories (Draft)**
*(To be refined with @product_team)*
1. **As a user**, I want to see **task metadata** (ID, priority, status) in a **collapsible panel** so I can focus on content.
   - *Acceptance:* Panel collapses/expands with animation (e.g., `transition: height 0.3s ease`).
2. **As a power user**, I want to **drag-and-drop** tasks between panels without losing context.
   - *Acceptance:* Drag state shows **visual feedback** (e.g., ghosting, progress bar).
3. **As a CEO**, I want the panel to **auto-highlight** critical tasks (e.g., `priority: high`) for quick triage.
   - *Acceptance:* Uses `Badge` component with Aura’s **error-red** token (`#FF3333`).

---

### **3. Adversarial UX Test Plan**
| **Test Case**               | **Expected Behavior**                                                                 | **Tool**               |
|-----------------------------|---------------------------------------------------------------------------------------|------------------------|
| Rapid clicks on "Edit"      | Debounce input (300ms delay) + confirmation dialog.                                   | Cypress                |
| Empty task list             | Show **illustrated empty state** (e.g., Baoyu-style "No tasks yet").                  | `baoyu-article-illustrator` |
| Invalid task ID input       | Reject with **inline validation** (e.g., `input:invalid { border: 1px solid #FF3333 }`). | Shadcn `Form`          |
| Keyboard-only navigation    | Tab through all interactive elements (no mouse dependency).                          | axe DevTools           |

---

### **4. Design Mockup Requirements**
- **Tool:** `sketch` (2-3 variants: *compact*, *expanded*, *mobile*).
- **Deliverables:**
  - Figma/Excalidraw prototype with **interactive states** (hover, focus, disabled).
  - **Design tokens spec** (e.g., `spacing: { panel: 24px }`, `borderRadius: 8px`).
- **Rejection Criteria:**
  - If mockup lacks **micro-interactions** (e.g., hover effects on buttons).
  - If typography doesn’t match **Claude’s `Inter` font stack**.

---
### **5. Escalation Path**
| **Risk**                          | **Action**                                                                 |
|-----------------------------------|----------------------------------------------------------------------------|
| PRD missing user stories          | Escalate to @product_team for alignment.                                  |
| Design conflicts with brand       | Escalate to @growth_director (Vesper) for naming/brand approval.         |
| Technical feasibility unknown     | Escalate to @tech_director (Vulcan) for architecture review.               |

---
### **6. Next Steps**
1. **Validate PRD:** Confirm `PRD-TDP-001` exists and is signed off.
2. **Design Review:** Generate 3 mockup variants using `sketch` + `shadcn`.
3. **UX Audit:** Run adversarial tests with Cypress/axe.
4. **CEO Signoff:** Present visuals and heuristics compliance to @ceo.

---
**Aura’s Note:**
*"A task panel without UX rigor is just a database dump. Let’s make it **premium**, **intuitive**, and **unbreakable**."*

---
**Status:** ✅ **Ready for PRD alignment**
**Next Action:** `@product_team` → Approve user stories by **2026-08-23**.
---
