# DPIA — Valutazione d'Impatto sulla Protezione dei Dati

**Progetto:** met-me / Mac — bot Telegram di supporto emotivo
**Titolare del trattamento:** Matteo Pozzi — pozzi.teo@gmail.com
**Versione documento:** 1.0
**Data:** 2026-05-22

---

## Premessa normativa

L'art. 35 GDPR impone al titolare del trattamento di effettuare una valutazione d'impatto sulla protezione dei dati (DPIA) quando un trattamento, considerato il suo tipo, oggetto, contesto e finalità, **può presentare un rischio elevato per i diritti e le libertà delle persone fisiche**.

La DPIA è obbligatoria in particolare per il trattamento su larga scala di **categorie particolari di dati** (art. 9), tra cui i **dati relativi alla salute**, che includono la salute mentale.

Il presente documento è il template iniziale di DPIA per il bot **met-me**. Va completato, riesaminato periodicamente (almeno annualmente) e ogni qualvolta intervengano modifiche sostanziali al trattamento.

---

## 1. Descrizione sistematica del trattamento

### 1.1 Natura, ambito e finalità

- **Cosa fa il bot:** offre uno spazio conversazionale di ascolto e supporto emotivo via Telegram. L'utente scrive messaggi liberi, il bot risponde tramite LLM.
- **Finalità:** fornire un servizio di ascolto e accompagnamento emotivo non-clinico, accessibile e a basso attrito. Il bot **non è** un servizio sanitario, medico o psicologico.
- **Modalità:** chat asincrona 1-a-1 tramite client Telegram dell'utente, backend Python su sistema gestito dal titolare.
- **Ambito:** utenti maggiorenni (18+), bilingue italiano/inglese (con l'LLM che si adatta a qualsiasi lingua), distribuzione iniziale in beta privata.

### 1.2 Dati trattati

| Categoria | Descrizione | Categoria particolare? |
|---|---|---|
| Identificativi Telegram | ID utente, username, nome, cognome | No |
| Contenuto conversazioni | Messaggi utente + risposte bot | **Sì (salute mentale — art. 9)** |
| Punteggi emotivi | Score 0–10 generati da LLM su ogni messaggio | **Sì (salute mentale — art. 9)** |
| Metadati operativi | Timestamp, modello LLM utilizzato | No |
| Record consenso | (user_id, versione policy, timestamp) | No |

### 1.3 Soggetti coinvolti

- **Titolare:** Matteo Pozzi.
- **Responsabili del trattamento (art. 28):**
  - **Telegram FZ-LLC** — piattaforma di messaggistica.
  - **Anthropic, PBC** — provider LLM (Claude) per la generazione delle risposte. Trasferimento dati negli Stati Uniti, coperto da Data Processing Addendum e Standard Contractual Clauses.
- **Interessati:** utenti finali del bot, maggiorenni.

---

## 2. Necessità e proporzionalità

### 2.1 Base giuridica

- **Art. 6(1)(a) GDPR — consenso** per il trattamento ordinario.
- **Art. 9(2)(a) GDPR — consenso esplicito** per i dati relativi alla salute mentale.

Il consenso è raccolto al primo utilizzo tramite tastiera inline Telegram (accept / decline). Senza consenso il bot non elabora messaggi. La revoca è esercitabile in qualsiasi momento tramite `/forget`.

### 2.2 Proporzionalità

- I dati raccolti sono **minimi e funzionali** alla generazione delle risposte conversazionali.
- L'utente decide liberamente cosa condividere; non vengono richiesti dati clinici, identificativi reali, contatti, dati di terzi.
- I metadati Telegram (ID, username, nome) sono ricevuti dalla piattaforma e necessari per indirizzare le risposte.
- Non vi è profilazione automatizzata che produca effetti giuridici sull'interessato (art. 22).

### 2.3 Misure di trasparenza

- Informativa privacy completa pubblicata in [PRIVACY.md](PRIVACY.md), accessibile sia via web sia inline tramite il comando `/privacy`.
- Comando `/forget` per la cancellazione self-service.
- Welcome esplicito + consent prompt che spiega: che cosa viene salvato, chi può leggere (admin), che è inviato a un provider LLM esterno, che non è servizio medico, requisito 18+, link alla policy completa.

---

## 3. Rischi per i diritti e le libertà degli interessati

| # | Rischio | Probabilità | Gravità | Punteggio |
|---|---|---|---|---|
| R1 | Accesso non autorizzato al DB locale (contenuti sensibili esposti) | _da valutare_ | Alta | _da valutare_ |
| R2 | Compromissione delle credenziali API (Telegram token, Anthropic key) | _da valutare_ | Media | _da valutare_ |
| R3 | Inadeguatezza della risposta del bot in situazione di crisi acuta (utente in pericolo) | Bassa-media | Alta | _da valutare_ |
| R4 | Trasferimento dati extra-UE (Anthropic) verso giurisdizione con livello di tutela differente | Certa | Media | _da valutare_ |
| R5 | Utenti minorenni che bypassano il check 18+ (auto-dichiarato) | Media | Alta | _da valutare_ |
| R6 | Re-identificazione utente dal contenuto delle conversazioni in caso di leak | Bassa | Alta | _da valutare_ |
| R7 | Risposte LLM allucinatorie/dannose (suggerimenti pericolosi) | Bassa-media | Alta | _da valutare_ |
| R8 | Perdita o corruzione del DB → indisponibilità servizio | Media | Bassa | _da valutare_ |
| R9 | Spam/abuso a fini di esfiltrazione costi (DoS economico) | Bassa | Bassa | _da valutare_ |
| R10 | Inadempimento richieste di esercizio diritti (es. portabilità) | Bassa | Media | _da valutare_ |

_Compilare le colonne Probabilità / Gravità / Punteggio una volta valutati concretamente nel proprio contesto operativo._

---

## 4. Misure di mitigazione

| Rischio | Misure attualmente implementate | Misure da implementare |
|---|---|---|
| R1 | DB SQLite su sistema controllato dal titolare. File `.env` e DB non versionati. | Cifratura del DB at-rest, controllo accessi al sistema, audit log accessi. |
| R2 | Token in `.env` gitignored. Rotazione token Telegram effettuata. Cap di spesa configurato su Anthropic. | Vault per i secret in produzione, rotazione periodica, monitoring uso anomalo. |
| R3 | Detection keyword di crisi → invio numeri verdi prima della risposta LLM. System prompt LLM addestrato a riconoscere crisi e indirizzare a emergenza. Rock-bottom tracker con alert agli admin. | Espansione keyword multilingua, procedura formale di follow-up admin su alert. |
| R4 | Anthropic DPA + Standard Contractual Clauses. Trasferimento dichiarato in informativa. | Valutazione opzioni provider UE-based. |
| R5 | Consent prompt richiede dichiarazione 18+. Comunicazione che il bot è destinato a maggiorenni. | _Limite intrinseco dell'auto-dichiarazione. Considerare verifica età solo se rischio si materializza._ |
| R6 | Nessun dato anagrafico raccolto oltre i metadati Telegram (volontari). | Pseudonimizzazione dei log se condivisi per debug. |
| R7 | System prompt vincola tono e contenuti. Disclaimer "non sostituisce professionista" nel consenso e nel prompt. Risposte brevi richieste al modello. | Test periodici red-team, review campionaria conversazioni con consenso aggiuntivo. |
| R8 | DB locale SQLite con journaling WAL. | Backup periodico off-site cifrato (pianificato post-pubblicazione). |
| R9 | Rate limit burst (5 msg / 10s in-memory). Cap di spesa Anthropic. | Sostained rate limit a livello orario se necessario. |
| R10 | `/forget` operativo. Esercizio diritti via email titolare documentato in informativa. | Procedura e SLA documentati per richieste via email. |

---

## 5. Consultazione del DPO e degli interessati

- **DPO designato?** No (non obbligatorio nel contesto di beta privata gestita da persona fisica, da rivalutare in caso di crescita o trattamento sistematico su larga scala — art. 37).
- **Consultazione interessati?** Non effettuata in fase di beta privata; auspicabile durante la fase di apertura pubblica.
- **Consultazione preventiva al Garante (art. 36)?** Da valutare se il rischio residuo dopo le mitigazioni resta elevato.

---

## 6. Conclusioni

_Da completare al termine della valutazione dei rischi (sezione 3) e delle misure (sezione 4)._

Dichiarare:

- se il trattamento può procedere alle attuali condizioni (rischio residuo accettabile),
- quali misure aggiuntive sono prerequisito al lancio pubblico,
- prossima data di revisione del documento.

---

## 7. Revisioni

| Versione | Data | Autore | Modifiche |
|---|---|---|---|
| 1.0 | 2026-05-22 | Matteo Pozzi | Versione iniziale, template da completare. |
