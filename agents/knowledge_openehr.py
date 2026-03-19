"""
openEHR Reference Model knowledge — injected into agent system prompts.

Covers:
  - Composition-level RM fields (composer, language, territory, category, context)
  - Entry types: OBSERVATION, EVALUATION, INSTRUCTION/ACTIVITY, ACTION/ISM_TRANSITION, ADMIN_ENTRY
  - Flat JSON ctx shortcuts
"""

OPENEHR_RM_KNOWLEDGE = """
═══════════════════════════════════════════════════════════
openEHR REFERENCE MODEL (RM) — COMPOSITION METADATA
═══════════════════════════════════════════════════════════
Every openEHR composition has RM-level metadata. Know what each field means:

COMPOSITION-LEVEL:
  composer/name              The clinician who AUTHORED this composition (e.g. "Dr. Anna Müller")
                             NOT the patient. Use the treating physician from the narrative.
  language/code_string       ISO 639-1 language code: "en", "de", "fr" — matches data language
  language/terminology_id    Always "ISO_639-1"
  territory/code_string      ISO 3166-1 alpha-2 country: "DE", "US", "GB", "AT", "CH" etc.
  territory/terminology_id   Always "ISO_3166-1"
  category                   openehr::433|event|      → encounters, observations, measurements
                             openehr::431|persistent| → ongoing records (problem list, med list)
                             openehr::432|episodic|   → episodic records (discharge summaries)

EVENT CONTEXT (present when category = event):
  context/start_time         ISO 8601 — when the clinical encounter/event STARTED
                             (e.g. patient arrival time, time measurement was taken)
  context/end_time           ISO 8601 — when the encounter ENDED (optional)
  context/setting            openehr::225|home|
                             openehr::227|primary medical care|
                             openehr::229|secondary medical care|
                             openehr::230|secondary nursing care|
                             openehr::238|other care|
  context/health_care_facility/name   Name of the hospital or clinic

SUBJECT (who the data is about):
  subject/_type              "PARTY_SELF"  — the EHR owner (the patient)

TIME FIELDS:
  Paths ending in /time or /date_time → ISO 8601 (e.g. "2024-03-15T09:30:00+02:00")
  Use times consistent with context/start_time and the clinical narrative.
  Never use future dates relative to the encounter.

FLAT JSON ctx shortcuts:
  ctx/template_id                The template ID string
  ctx/language                   ISO 639-1 code (e.g. "en")
  ctx/territory                  ISO 3166-1 code (e.g. "DE")
  ctx/time                       Composition authoring time (ISO 8601)
  ctx/composer_name              Authoring clinician's name
  ctx/health_care_facility_name  Facility name
  ctx/id_scheme                  "local"
  ctx/id_namespace               "local"

═══════════════════════════════════════════════════════════
openEHR ENTRY TYPES — SEMANTICS AND MANDATORY FIELDS
═══════════════════════════════════════════════════════════
Each archetype in a composition is one of these entry types. The type determines what
structural fields are required around the clinical content.

ALL ENTRIES share (in addition to their own content):
  language/code_string        ISO 639-1  (usually same as composition language)
  language/terminology_id     "ISO_639-1"
  encoding/code_string        "UTF-8"
  encoding/terminology_id     "IANA_character-sets"
  subject/_type               "PARTY_SELF"
  provider/name               Clinician responsible for this entry

──────────────────────────────────────────────────────────
OBSERVATION  — clinical measurements and findings over time
  Used for: vital signs, lab results, ECG, imaging findings, questionnaire scores

  data/events[at0006]/time    ISO 8601 — when the measurement was taken (POINT_EVENT)
  data/events[at0006]/data    The measured values (BP, glucose, temperature, etc.)
  data/events[at0006]/state   Patient state during measurement (resting, fasting) — optional
  protocol/...                How measured (device, method) — optional

  INTERVAL_EVENT (covers a period, e.g. 24h average):
  data/events[at0007]/time          Start of the interval
  data/events[at0007]/width         Duration as ISO 8601 duration (e.g. "PT24H")
  data/events[at0007]/math_function openehr::146|mean|  openehr::144|maximum|  etc.

──────────────────────────────────────────────────────────
EVALUATION  — clinical assessments and conclusions
  Used for: diagnoses, problem list entries, risk assessments, allergy records,
            clinical summaries, family history, goal statements

  data/...         The assessment content (diagnosis, severity, status, etc.)
  protocol/...     Basis for the assessment — optional

  NOTE: Evaluations represent a clinician's CONCLUSION, not a raw measurement.
  The encounter time comes from composition context/start_time.

──────────────────────────────────────────────────────────
INSTRUCTION  — orders for future actions
  Used for: medication prescriptions, procedure orders, referrals, care plans

  narrative                              Human-readable order summary (mandatory DV_TEXT)
  activities[at0001]/description/...     What to do (order content)
  activities[at0001]/timing              When/how often — DV_PARSABLE, HL7 v3 GTS syntax:
                                         "R1PT6H" = once every 6h
                                         "R3/P1D"  = 3× per day
                                         Plain text also accepted: "Once daily in the morning"
  activities[at0001]/action_archetype_id Regex matching the ACTION archetype, e.g.
                                         "openEHR-EHR-ACTION.medication.v1"
  expiry_time                            ISO 8601 — when the order expires (optional)

──────────────────────────────────────────────────────────
ACTION  — record of something actually done
  Used for: medication administration, procedures performed, care steps taken

  time                  ISO 8601 — when the action was performed (mandatory)
  description/...       What was actually done

  ism_transition (MANDATORY — the workflow state in the Instruction State Machine):
    current_state|value       "initial"    openehr::524  order created, not yet scheduled
                              "scheduled"  openehr::529  planned for a specific time
                              "postponed"  openehr::527  delayed
                              "cancelled"  openehr::528  will not happen
                              "active"     openehr::245  currently in progress
                              "suspended"  openehr::530  temporarily on hold
                              "aborted"    openehr::531  stopped before completion
                              "completed"  openehr::532  done ← most common
    current_state|code        The openehr code (e.g. "532")
    current_state|terminology "openehr"
    careflow_step|value       Local label for this step (e.g. "Medication administered")
    careflow_step|code        at-code from the archetype (e.g. "at0016")
    careflow_step|terminology "local"

  instruction_details (optional — links back to the originating INSTRUCTION):
    instruction_details/instruction_id/path   Path to the instruction
    instruction_details/activity_id           "activities[at0001]"

  ISM state selection guide:
    Drug prescribed only           → scheduled
    Drug administered in hospital  → completed
    Procedure performed            → completed
    Procedure cancelled            → cancelled
    Infusion currently running     → active
    Treatment on hold              → suspended
    Treatment stopped early        → aborted

──────────────────────────────────────────────────────────
ADMIN_ENTRY  — administrative (non-clinical) data
  Used for: admissions, discharges, appointments, registration events
  No special RM fields beyond the common ENTRY fields.
""".strip()
