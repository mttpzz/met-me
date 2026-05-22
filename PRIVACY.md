# Informativa Privacy — met-me / Mac

**Versione:** 1.2
**Ultimo aggiornamento:** 2026-05-22

Questa informativa descrive come vengono trattati i dati personali degli utenti del bot Telegram **met-me** (di seguito "il bot" o "Mac") ai sensi del **Regolamento UE 2016/679 ("GDPR")** e del **D.lgs. 196/2003 e successive modificazioni ("Codice Privacy" italiano)**.

---

## 1. Titolare del trattamento

**Matteo Pozzi**
Contatto: [pozzi.teo@gmail.com](mailto:pozzi.teo@gmail.com)

Per esercitare i diritti previsti dalla normativa o per qualsiasi richiesta relativa al trattamento dei dati, scrivere all'indirizzo email indicato.

---

## 2. Natura dei dati trattati

Il bot raccoglie e tratta:

- **Dati identificativi forniti da Telegram**: ID utente Telegram, username (se impostato), nome e cognome (se impostati).
- **Contenuto delle conversazioni**: tutti i messaggi di testo inviati dall'utente al bot e le risposte generate dal bot.
- **Punteggi di stato emotivo**: per ogni messaggio utente, un classificatore basato su LLM assegna un punteggio numerico (0–10) che stima il tono emotivo del messaggio. Tali punteggi sono conservati e utilizzati per generare alert agli amministratori quando una media mobile scende sotto una soglia configurata.

I contenuti delle conversazioni e i punteggi associati **possono includere dati relativi alla salute mentale dell'utente** e sono pertanto qualificabili come **categorie particolari di dati personali** ai sensi dell'art. 9 GDPR.

---

## 3. Finalità e base giuridica

| Finalità | Base giuridica |
|---|---|
| Fornire un servizio conversazionale di ascolto e supporto emotivo | Consenso esplicito dell'interessato (**art. 6(1)(a)** e **art. 9(2)(a) GDPR**) |
| Migliorare la qualità del servizio e prevenire abusi (ban di utenti che violano i termini d'uso, registro delle azioni amministrative) | Legittimo interesse del titolare (**art. 6(1)(f) GDPR**) |
| Adempiere a obblighi di legge (es. richieste di autorità) | Obbligo legale (**art. 6(1)(c) GDPR**) |

Il consenso è raccolto al primo utilizzo del bot tramite un'apposita schermata. Senza consenso il bot non elabora messaggi.

---

## 4. Modalità del trattamento

- I dati sono conservati in un **database SQLite locale** sul sistema gestito dal titolare.
- L'accesso al database è ristretto al titolare e agli amministratori designati.
- I contenuti delle conversazioni vengono inviati al provider LLM configurato (vedi sezione "Destinatari") per generare le risposte.
- Il file di log per ciascun utente conserva i messaggi in chiaro fino a esercizio del diritto di cancellazione o purga automatica.

---

## 5. Destinatari e sub-responsabili

I dati possono essere comunicati ai seguenti soggetti, che agiscono come **responsabili del trattamento** ai sensi dell'art. 28 GDPR:

| Soggetto | Ruolo | Sede | Garanzie |
|---|---|---|---|
| **Telegram FZ-LLC** | Piattaforma di messaggistica | Emirati Arabi Uniti / Regno Unito | Privacy policy Telegram ([telegram.org/privacy](https://telegram.org/privacy)) |
| **Anthropic, PBC** | Provider LLM (Claude) per la generazione delle risposte | Stati Uniti d'America | Data Processing Addendum sottoscritto, Standard Contractual Clauses (SCC) per il trasferimento extra-UE |
| **Functional Software, Inc. d/b/a Sentry** | Servizio di error monitoring (raccolta automatica di eccezioni e stack trace per diagnostica) | Region EU (Francoforte, Germania) | Data Processing Addendum standard Sentry; nessun trasferimento extra-UE in configurazione attuale. Dati inviati: tipo eccezione, messaggio errore, stack trace, file/riga/funzione, tag ambiente, `request_id` casuale di 12 caratteri esadecimali (pseudonimo non riconducibile all'utente, usato solo per correlazione con i log interni del titolare). NON inviati: ID utente Telegram, username, nome, contenuto messaggi. |

I dati **non sono ceduti, venduti o comunicati a terze parti** al di fuori di quanto sopra elencato e fatti salvi obblighi di legge.

---

## 6. Trasferimento extra-UE

L'utilizzo del provider Anthropic comporta il trasferimento di dati negli **Stati Uniti**. Tale trasferimento avviene sulla base delle **Clausole Contrattuali Standard (SCC)** approvate dalla Commissione Europea, che garantiscono un livello di protezione adeguato.

---

## 7. Conservazione

| Dato | Periodo di conservazione |
|---|---|
| Profilo utente e messaggi | Massimo `MESSAGE_RETENTION_DAYS` giorni (default 180), poi purga automatica mensile (art. 5(1)(c) GDPR — minimizzazione) |
| Punteggi di stato emotivo | Stessa retention di messaggi e log per-utente |
| Log per-utente | Stessa retention; le righe più vecchie della soglia vengono rimosse, e i file rimasti vuoti cancellati |
| Record di consenso | 5 anni dalla revoca o cancellazione (come prova ai sensi dell'art. 7 GDPR) |
| Ban (`banned_users`) | Conservati a tempo indeterminato anche dopo `/forget` dell'utente sanzionato: legittimo interesse del titolare (art. 6(1)(f)) all'efficacia delle misure anti-abuso. Possono essere rimossi su richiesta motivata via email. |
| Audit log delle azioni amministrative (`admin_audit`) | Conservato a tempo indeterminato per finalità di accountability (art. 24/32 GDPR). Ogni riga registra `admin_id`, azione, eventuale utente bersaglio e timestamp. |

L'utente può richiedere in qualsiasi momento la cancellazione di tutti i dati conversazionali tramite il comando `/forget` all'interno del bot (vedi sezione "Diritti dell'interessato"). Ban e audit log sopravvivono alla cancellazione conversazionale per i motivi sopra indicati.

---

## 8. Diritti dell'interessato

Ai sensi degli **artt. 15–22 GDPR**, l'utente ha diritto di:

- **Accedere** ai propri dati (art. 15)
- **Rettificare** dati inesatti (art. 16)
- **Cancellare** i dati ("diritto all'oblio", art. 17) — esercitabile direttamente con `/forget`
- **Limitare** il trattamento (art. 18)
- **Portabilità** dei dati (art. 20) — su richiesta via email
- **Opporsi** al trattamento basato su legittimo interesse (art. 21)
- **Revocare il consenso** in qualsiasi momento (art. 7), senza pregiudicare la liceità del trattamento precedente

Per esercitare tali diritti, utilizzare i comandi `/privacy` e `/forget` nel bot oppure scrivere a [pozzi.teo@gmail.com](mailto:pozzi.teo@gmail.com).

L'utente ha inoltre diritto di proporre reclamo all'autorità di controllo competente: in Italia il **Garante per la protezione dei dati personali** ([gpdp.it](https://www.gpdp.it/)).

---

## 9. Età minima

Il bot è destinato esclusivamente a **persone maggiorenni (18+ anni)**. Non è progettato per essere utilizzato da minori e non è oggetto di consenso genitoriale ai sensi dell'art. 8 GDPR. L'utente conferma il requisito di età all'atto del consenso iniziale.

---

## 10. Servizio non sostitutivo

Il bot **non è un servizio medico, sanitario o psicologico** e **non sostituisce in alcun modo il parere di un professionista qualificato**. In presenza di situazioni di disagio significativo, crisi acuta o ideazione suicidaria, l'utente è invitato a rivolgersi immediatamente a:

- 📞 **Telefono Amico Italia** — 02 2327 2327 (tutti i giorni 10:00–24:00)
- 📞 **Samaritans Onlus** — 800 86 00 22 (tutti i giorni 13:00–22:00)
- 📞 **Telefono Azzurro** (minori) — 19696 (24/7, gratuito)
- 🚑 **Emergenza sanitaria** — 112

---

## 11. Misure di sicurezza

- Database conservato su sistema controllato dal titolare, accessi ristretti.
- Token e credenziali API conservati come variabili d'ambiente, mai versionate in repository pubblico.
- Comunicazione con Telegram, Anthropic e Sentry via TLS.
- Limite di frequenza dei messaggi (rate limit burst, in-memory) per ridurre rischi di flooding e DoS economico.
- **Audit log amministrativo** (`admin_audit`): ogni comando di amministrazione viene registrato con timestamp, identificativo dell'amministratore e parametri pertinenti, per finalità di accountability (art. 24/32 GDPR). Anche la consultazione dell'audit è tracciata.
- Cancellazione automatica mensile dei dati conversazionali oltre la soglia di retention configurata.

---

## 12. Modifiche all'informativa

Eventuali modifiche sostanziali a questa informativa comportano l'incremento della **versione** indicata in testa al documento. Agli utenti già registrati sarà richiesto un **nuovo consenso esplicito** prima di poter continuare a utilizzare il bot.

Modifiche minori (refusi, chiarimenti non sostanziali) sono pubblicate con aggiornamento della data senza richiedere nuovo consenso.

---

## 13. Contatti

Per qualsiasi richiesta o chiarimento:

**Matteo Pozzi**
Email: [pozzi.teo@gmail.com](mailto:pozzi.teo@gmail.com)
Repository: [github.com/mttpzz/met-me](https://github.com/mttpzz/met-me)
