# Fonely — AI Business Assistant for Indian MSMEs

## One-Line Pitch

An AI assistant that answers phone calls and manages orders/appointments for Indian small businesses — in their customer's language, 24/7. Setup via WhatsApp in 10 minutes. No app, no dashboard, no English required.

---

## The Problem

63 million small businesses in India — meat shops, dental clinics, bakeries, salons, vegetable vendors — all share the same problems:

1. **Can't answer every call.** The meat shop owner's hands are bloody. The dentist is with a patient. The salon stylist is mid-haircut. 62% of calls go unanswered. 85% of those callers never call back.

2. **No way to manage orders/bookings remotely.** A customer wants to reserve 2kg chicken for pickup at 8am. Today that requires a phone call the owner might miss. There's no system — just a notebook and memory.

3. **Can't afford a receptionist.** A human receptionist costs ₹15,000-25,000/month. No MSME can afford that for after-hours or busy-hours coverage.

4. **Technology is too complex.** Existing solutions (Skit.ai, Ringg, Bolna) target enterprises with dashboards, APIs, CRM integrations. A meat shop owner doesn't have a CRM. Their system is WhatsApp and a notebook.

---

## The Solution

Fonely gives every small business an AI assistant that:
- **Answers their phone** in the customer's language (Tamil, Hindi, Telugu, Kannada, etc.)
- **Takes orders** with real-time stock tracking (meat shops, bakeries, vegetable vendors)
- **Books appointments** with real-time slot management (clinics, salons, tutors)
- **Notifies the owner** via WhatsApp for every order/booking
- **Lets the owner manage everything** by just talking to WhatsApp — update stock, block times, change prices

**Setup takes 10 minutes via WhatsApp. No app download. No training. No English required.**

---

## Three Core Flows

### Flow 1: Owner Onboarding (WhatsApp, one-time, ~5 minutes)

```
Owner opens Fonely WhatsApp bot
    │
    ▼
Bot: "Welcome to Fonely! Select your language"
     [Tamil] [Hindi] [Telugu] [Kannada] [English]
    │
    ▼
Bot: "Unga kadai peyar enna?" (What is your shop name?)
Owner: "Rajan Meat Shop"
    │
Bot: "Enna vikareenga?" (What do you sell?)
Owner: "Chicken, mutton"
    │
Bot: "Kadai timing enna?" (Shop timing?)
Owner: "5am to 10am"
    │
Bot: "Location share pannunga" (Share your location)
Owner: [shares Google Maps pin]
    │
Bot: "Prices sollunga" (Tell me your prices)
Owner: "Chicken 220/kg, mutton 750/kg"
    │
    ▼
Bot: "₹499/month. Pay here:" [Razorpay UPI link]
    │
    ▼ (payment confirmed)
    │
Bot: "Ready! Unga AI assistant phone number: 044-XXXX-XXXX
     Kadai la indha number ah display pannunga.
     Customers call panna, naan handle pannuren.

     Stock update panna ennoda chat la sollunga."
```

### Flow 2: Owner Daily Management (WhatsApp, natural language)

The owner talks to the Fonely bot anytime via WhatsApp to manage their business:

```
MORNING — Owner updates stock:
Owner: "Innaiku 5kg chicken irukku, 3kg mutton irukku"
Bot:   "Updated!
        Chicken: 5kg @ ₹220/kg
        Mutton: 3kg @ ₹750/kg
        I'll take orders until stock finishes."

MID-DAY — Owner updates after walk-in sales:
Owner: "2kg chicken sold"
Bot:   "Updated! Chicken: 3kg remaining.
        2 phone orders pending pickup."

TIMING CHANGE:
Owner: "Naalaikku 6am la thaan kadai thiappein"
Bot:   "OK. Tomorrow opening at 6am instead of 5am.
        I'll inform anyone who calls."

ITEM UNAVAILABLE:
Owner: "Naalaikku mutton varaathu"
Bot:   "OK. Tomorrow mutton not available.
        I'll let customers know."

DAY END — Auto summary:
Bot:   "Today's summary:
        12 calls, 8 orders, 2 enquiries
        Chicken: 0kg remaining (all sold)
        Mutton: 1kg remaining
        Revenue: ₹4,850
        Tomorrow: Mutton unavailable"
```

### Flow 3: Customer Calls the Shop Number

```
Customer calls 044-XXXX-XXXX
    │
    ▼
AI (Tamil): "Vanakkam! Rajan Meat Shop. Enna venum?"

Customer: "Innaiku chicken irukka?"
AI: "Irukku! 3kg available. Kg 220 rooba. Evalo venum?"

Customer: "2kg venum"
AI: "Sari. 2kg chicken, total 440 rooba.
     Enga maniku vareenga pick up panna?"

Customer: "8 maniku"
AI: "Sari, confirm pannunga:
     Chicken 2kg — ₹440
     Pickup kaaalai 8 mani
     Correct ah?"

Customer: "Sari"
AI: "Done! 2kg chicken 8 maniku ready ah irukum.
     Unga peyar sollunga?"

Customer: "Suresh"
AI: "Sari Suresh, 8 maniku vaanga. Nandri!"
    │
    ▼
Owner gets WhatsApp:
"📦 New Order
 Customer: Suresh (98765xxxxx)
 Item: Chicken 2kg — ₹440
 Pickup: 8:00 AM
 Stock remaining: Chicken 1kg"

Customer gets WhatsApp:
"✅ Order confirmed — Rajan Meat Shop
 Chicken 2kg — ₹440
 Pickup: 8:00 AM
 Address: [shop address]"
```

### Same System, Different Business Types

```
business_type: "meat_shop"
  → inventory tracking, orders, pickup times
  → owner updates stock daily
  → AI manages quantity deduction

business_type: "dental_clinic"
  → appointment slots, bookings
  → owner sets hours and blocked days
  → AI manages slot availability
  → emergency escalation to doctor

business_type: "bakery"
  → inventory (cakes, bread) + orders
  → "today's specials" from owner
  → AI takes orders with pickup time

business_type: "salon"
  → appointment slots by service type
  → different durations (haircut 30min, facial 60min)
  → AI manages slot conflicts

business_type: "vegetable_vendor"
  → daily changing inventory and prices
  → bulk orders, delivery time slots
  → seasonal item availability

business_type: "tuition_center"
  → class schedule, seat availability
  → trial class booking
  → fee enquiry
```

---

## Architecture: How LLM and Data Work Together

### The Core Principle

**LLM is the ears and mouth. Database is the brain. Code is the gatekeeper.**

The LLM NEVER stores business data (stock, orders, appointments). The LLM NEVER calculates prices. The LLM NEVER tracks inventory. All business logic runs through deterministic code that reads/writes a database.

### The Data Flow

```
Customer speaks
    │
    ▼
Sarvam STT (speech → text)
    │
    ▼
LLM (understands intent → outputs structured JSON action)
    │
    │   Example LLM output:
    │   {
    │     "action": "place_order",
    │     "item": "chicken",
    │     "qty_kg": 2,
    │     "pickup_time": "08:00"
    │   }
    │
    ▼
YOUR CODE (validates against database, executes if valid)
    │
    │   Code does:
    │   1. SELECT available_qty FROM inventory WHERE item='chicken' → 5kg
    │   2. Check: 2 <= 5? → YES
    │   3. INSERT INTO orders (item, qty, ...) VALUES ('chicken', 2, ...)
    │   4. UPDATE inventory SET available_qty = available_qty - 2
    │   5. Return: { success: true, total: 440, remaining: 3 }
    │
    ▼
LLM (reads code's result → generates natural response)
    │
    │   LLM says: "Sari! 2kg chicken, 440 rooba, 8 maniku ready"
    │
    ▼
Sarvam TTS (text → speech)
    │
    ▼
Customer hears response
```

### Tool Calling Schema

The LLM has these tools available. It picks the right one based on conversation:

```json
TOOLS:

check_stock(item?)
  → Returns: { items: [{ name, available_qty, unit, price }] }
  → Used when: customer asks "chicken irukka?", "enna irukku?"

place_order(item, qty, unit, pickup_time, customer_name?)
  → Validates: stock >= qty, pickup_time within hours
  → Returns: { success, total_amount, remaining_stock }
  → Used when: customer says "2kg venum"

check_slots(date?, service?)
  → Returns: { available_slots: ["10:00", "11:00", ...] }
  → Used when: patient asks "naalaikku appointment irukka?"

book_appointment(date, time, service, patient_name, reason?)
  → Validates: slot is available, within hours
  → Returns: { success, confirmed_time }
  → Used when: patient says "10 maniku book pannu"

get_business_info(question_type)
  → Returns: { address, hours, services, prices }
  → Used when: customer asks "address enna?", "evalo charge?"

escalate_to_owner(reason, urgency)
  → Sends WhatsApp to owner immediately
  → Returns: { notified: true }
  → Used when: emergency, complaint, special request
```

### What Prevents Hallucination

| Guard | How |
|-------|-----|
| **LLM never stores numbers** | Database is the only truth. LLM reads from code result, never from memory. |
| **Fresh context every call** | At call start, code queries DB and injects current stock/slots into LLM prompt. No stale data. |
| **Schema-constrained output** | LLM must output valid JSON matching the tool schema. Free-text numbers are rejected. |
| **Code validates everything** | Stock >= order qty? Slot available? Time within hours? All checked by code, not LLM. |
| **Confirmation before execution** | AI reads back order details. Only executes after customer says "sari/yes/correct". |
| **Database-level locks** | `UPDATE stock SET qty = qty - 2 WHERE qty >= 2` — prevents two customers ordering the last stock. |
| **Owner sees every transaction** | WhatsApp notification for every order/booking. Owner can correct immediately. |

### What LLM Sees at Call Start

Code generates this context from the database fresh for every call:

```
"You are the AI assistant for Rajan Meat Shop.

 CURRENT STOCK (from database — NEVER make up numbers):
 - Chicken: 3kg available @ ₹220/kg
 - Mutton: 5kg available @ ₹750/kg

 TODAY'S ORDERS SO FAR:
 - Suresh: 2kg chicken, pickup 8am (pending)
 - Lakshmi: 1kg mutton, pickup 9am (picked up)

 SHOP HOURS: 5:00 AM to 10:00 AM
 CURRENT TIME: 7:30 AM
 TOMORROW: Mutton unavailable

 LANGUAGE: Respond in Tamil. Do not mix English.

 RULES:
 - Always use check_stock before telling availability
 - Always confirm order details before place_order
 - Never guess quantities or prices
 - For anything you're unsure about, use escalate_to_owner"
```

---

## Tech Stack

| Component | Technology | Cost | Purpose |
|-----------|-----------|------|---------|
| Speech-to-Text | Sarvam Saaras v3 (streaming WebSocket) | ₹45/hour | Understand customer speech in 22 Indian languages |
| Text-to-Speech | Sarvam Bulbul v3 (HTTP streaming) | ₹30/10K chars | AI speaks back naturally in customer's language |
| LLM | Sarvam 105B (tool calling) | ₹4-16/1M tokens | Understand intent, fill tool schemas, generate responses |
| Telephony | Exotel AgentStream (bidirectional WebSocket) | ₹0.50-1.50/min | Phone number + live audio streaming |
| WhatsApp | WhatsApp Business API via Gupshup or Wati | ₹0.50-1/message | Owner onboarding, management, notifications |
| Payment | Razorpay | 2% per transaction | Subscription payment collection |
| Database | PostgreSQL (Supabase) | Free → ₹2000/month | Business data, inventory, orders, appointments |
| Server | Node.js/Bun on Railway or Render | ₹2000/month | Application server |

### Cost Per Call
```
Average call: 2 minutes, 6 conversation turns
STT:  2 min × ₹0.75/min                   = ₹1.50
LLM:  ~2000 tokens × ₹10/1M tokens        = ₹0.02
TTS:  ~600 characters × ₹30/10K chars      = ₹1.80
Telephony: 2 min × ₹1/min                  = ₹2.00
─────────────────────────────────────────────
Total per call:                              ~₹5.30
```

---

## Database Schema

```sql
-- Business owners
CREATE TABLE shops (
  id            SERIAL PRIMARY KEY,
  name          TEXT NOT NULL,
  type          TEXT NOT NULL,  -- meat_shop, dental_clinic, bakery, salon, etc.
  owner_name    TEXT,
  owner_phone   TEXT NOT NULL UNIQUE,
  language      TEXT DEFAULT 'ta-IN',
  address       TEXT,
  location_lat  DECIMAL,
  location_lng  DECIMAL,
  opening_time  TIME NOT NULL,
  closing_time  TIME NOT NULL,
  working_days  TEXT DEFAULT 'mon,tue,wed,thu,fri,sat',
  phone_number  TEXT,           -- assigned Exotel number
  subscription  TEXT DEFAULT 'trial',  -- trial, active, expired
  paid_until    DATE,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Dynamic inventory (shops that sell goods)
CREATE TABLE inventory (
  id             SERIAL PRIMARY KEY,
  shop_id        INT REFERENCES shops(id),
  item_name      TEXT NOT NULL,
  available_qty  DECIMAL NOT NULL DEFAULT 0,
  unit           TEXT DEFAULT 'kg',  -- kg, piece, dozen, litre
  price_per_unit DECIMAL NOT NULL,
  available_tomorrow BOOLEAN DEFAULT TRUE,
  updated_at     TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(shop_id, item_name)
);

-- Appointment slots (clinics, salons, tutors)
CREATE TABLE slots (
  id          SERIAL PRIMARY KEY,
  shop_id     INT REFERENCES shops(id),
  date        DATE NOT NULL,
  start_time  TIME NOT NULL,
  end_time    TIME NOT NULL,
  service     TEXT,
  status      TEXT DEFAULT 'available',  -- available, booked, blocked, walk_in
  booking_id  INT,
  UNIQUE(shop_id, date, start_time)
);

-- Orders (meat shops, bakeries, vendors)
CREATE TABLE orders (
  id             SERIAL PRIMARY KEY,
  shop_id        INT REFERENCES shops(id),
  customer_name  TEXT,
  customer_phone TEXT NOT NULL,
  items          JSONB NOT NULL,  -- [{item, qty, unit, price_per_unit, subtotal}]
  total_amount   DECIMAL NOT NULL,
  pickup_time    TIME,
  status         TEXT DEFAULT 'confirmed',  -- confirmed, picked_up, cancelled
  call_id        INT,
  ordered_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Appointments (clinics, salons)
CREATE TABLE bookings (
  id             SERIAL PRIMARY KEY,
  shop_id        INT REFERENCES shops(id),
  slot_id        INT REFERENCES slots(id),
  customer_name  TEXT,
  customer_phone TEXT NOT NULL,
  service        TEXT,
  reason         TEXT,
  status         TEXT DEFAULT 'confirmed',  -- confirmed, completed, no_show
  call_id        INT,
  booked_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Call logs
CREATE TABLE calls (
  id              SERIAL PRIMARY KEY,
  shop_id         INT REFERENCES shops(id),
  caller_phone    TEXT,
  language        TEXT,
  duration_sec    INT,
  outcome         TEXT,  -- ordered, booked, enquiry, out_of_stock, escalated
  transcript      JSONB,  -- [{role, text, timestamp}]
  started_at      TIMESTAMPTZ DEFAULT NOW(),
  ended_at        TIMESTAMPTZ
);
```

---

## Business Model

```
Pricing:
  Starter:  ₹499/month  — up to 100 calls, 1 phone number
  Growth:   ₹999/month  — up to 500 calls, priority support

Revenue per customer:  ₹499-999/month
Cost per customer:     ₹150-500/month (calls + APIs)
Gross margin:          50-85%

Target: 1000 customers = ₹5-10L/month revenue
```

---

## Competitive Positioning

| Them (Bolna, Skit.ai, Ringg, TringTring) | Fonely |
|------|------|
| Enterprise pricing ₹15K-50K/month | ₹499-999/month |
| Dashboard + CRM + API integration | WhatsApp-only — zero setup |
| Requires technical person to configure | Shop owner does it in 5 minutes |
| Sells to companies with IT departments | Sells to meat shop owners with notebooks |
| Generic voice bot platform | Complete shop assistant (inventory + orders + calls) |
| 1000 enterprise customers | 100,000 MSME customers |

---

## MVP Scope (4 Weeks)

### Week 1: Voice Pipeline
- [ ] Sarvam STT streaming (WebSocket) integration
- [ ] Sarvam TTS streaming (HTTP stream) for low latency
- [ ] LLM with tool calling schema
- [ ] End-to-end: speak → understand → respond in Tamil
- [ ] Browser demo that works on phone/laptop
- [ ] Test with meat shop and dental clinic scenarios

### Week 2: Tool Calling + Database
- [ ] PostgreSQL schema (shops, inventory, orders, slots, bookings, calls)
- [ ] Implement tool functions: check_stock, place_order, check_slots, book_appointment
- [ ] Confirmation flow (read back details, wait for "sari")
- [ ] Database-level guards (stock validation, slot conflicts, race conditions)
- [ ] Test: full order flow and appointment flow through voice

### Week 3: WhatsApp Integration
- [ ] WhatsApp Business API setup (Gupshup or Wati)
- [ ] Owner onboarding flow (language → business details → payment)
- [ ] Owner daily management (update stock, change timing, block days)
- [ ] Order/booking notifications to owner
- [ ] Confirmation messages to customer
- [ ] Daily summary report

### Week 4: Exotel + First Customers
- [ ] Exotel AgentStream integration (real phone calls)
- [ ] Phone number assignment per business
- [ ] Razorpay payment integration
- [ ] Sign up 5 businesses for free trial (2 meat shops, 2 clinics, 1 bakery)
- [ ] Monitor real calls, fix issues, iterate

---

## Growth Path

```
Month 1-3:   MVP + 10 free trial businesses → iterate on real calls
Month 3-6:   Start charging. Target 100 paying customers.
Month 6-9:   Expand to more business types. 5+ verticals.
Month 9-12:  1000 customers. ₹10L/month revenue.
             → Raise seed round (₹2-5 Cr)
Month 12+:   Build app for power users. Scale. Quit AMD.
```

---

## What We're NOT Building (MVP)

- No mobile app (WhatsApp only)
- No web dashboard
- No CRM integration
- No outbound calling
- No delivery coordination
- No payment collection from end customers
- No analytics beyond daily WhatsApp summary

All of these come later, driven by customer demand.

---

## Capital Required

| Item | Cost | When |
|------|------|------|
| Sarvam AI API (Starter plan) | Free (₹300 credits) | Now |
| Exotel account | ₹500 free credits | Now |
| WhatsApp Business API (Gupshup) | ~₹5,000 | Week 3 |
| Razorpay setup | Free | Week 4 |
| Server hosting (Railway/Render) | ~₹2,000/month | Week 1 |
| Database (Supabase free tier) | Free | Week 1 |
| Domain (fonely.ai or final name) | ~₹3,000 | When name is final |
| **Total to launch MVP** | **~₹15,000** | |

---

## Language Support

All customer-facing conversations support:
- Tamil (ta-IN)
- Hindi (hi-IN)
- Telugu (te-IN)
- Kannada (kn-IN)
- Malayalam (ml-IN)
- Bengali (bn-IN)
- Marathi (mr-IN)
- Gujarati (gu-IN)
- Punjabi (pa-IN)
- Odia (od-IN)
- English (en-IN)

Auto-detection: the STT detects the caller's language from their first sentence. The AI responds in the same language throughout the call. No language menu needed.

Owner management via WhatsApp works in whatever language they chose during onboarding.

---

## Key Design Decisions

1. **WhatsApp-first, not app-first.** 150x better activation rate. Build app only when customers ask for it.

2. **LLM for understanding, code for execution.** Zero hallucination risk on business data.

3. **Confirmation before every transaction.** AI reads back details, customer confirms. No silent orders.

4. **Owner manages via natural language.** "2kg chicken sold" not a dashboard form.

5. **One system, many business types.** Same tool-calling layer handles inventory shops AND appointment businesses.

6. **Start with Tamil + Chennai.** Prove it in one city, one language, then expand.

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Voice quality not natural enough | Users hang up | Use Bulbul v3 best speakers, 24kHz quality, test extensively |
| STT misunderstands quantity/item | Wrong order | Always confirm before executing. Owner sees every order. |
| Exotel latency too high | Unnatural conversation | Use streaming TTS, optimize prompt for short responses |
| Businesses won't pay ₹499/month | No revenue | Free trial → show call stats → "you recovered ₹X in orders" |
| WhatsApp Business API approval slow | Can't onboard | Start with manual onboarding, add WhatsApp later |
| Competition copies us | Lose differentiation | Move fast. Vertical depth (meat shop workflows) is the moat. |
| LLM API cost rises | Margin squeeze | Sarvam is cheapest. Can switch to open-source self-hosted later. |

---

## Next Immediate Steps

1. ✅ Sarvam API — working
2. ✅ Exotel account — created, waiting for AgentStream enablement
3. ✅ Browser demo — working (text input + voice response)
4. → Fix streaming TTS for lower latency
5. → Fix STT for mic-based voice input
6. → Build tool calling architecture
7. → Set up PostgreSQL database
8. → Follow up with Exotel Manas for AgentStream
9. → Sign up for WhatsApp Business API
