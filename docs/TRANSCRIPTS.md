# Example conversations (real runs)

Model: `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` · generated 2026-10-27 23:43 IST by `scripts/run_journeys.py`


## Recommendation → availability → booking

**Guest:** Suggest a romantic place for our anniversary this Saturday at 8 pm, around ₹1500 per person, 2 of us

> `recommend_restaurants({"budget_per_person": 1500, "date": "this saturday", "occasion": "anniversary", "party_size": 2, "time": "20:00"})` → ✅

**Goodie:** Here are three romantic options within your ₹1500 per‑person budget for Saturday 31 Oct at 8 pm for two:

1. **GoodFoods Mezze Bar – MG Road** (Mediterranean) – about ₹1,280 per person – rooftop setting with private‑dining option, rated 4.4.  
2. **GoodFoods Sakura – JP Nagar** (Japanese) – about ₹1,330 per person – private‑dining ambience, rated 4.6.  
3. **GoodFoods Tandoor House – Whitefield** (North Indian) – about ₹870 per person – rooftop with live music, rated 4.9.  

All three have a free table for two at 8 pm on Saturday 31 Oct.  

Which one would you like to reserve?

**Guest:** The first one sounds good, please check it

> `check_availability({"time": "20:00", "party_size": 2, "date": "2026-10-31", "restaurant_id": "GF056"})` → ✅

**Goodie:** Great! To lock in the reservation at GoodFoods Mezze Bar - MG Road for Saturday 31 Oct at 8 pm for two, could you please share:

1. Your name (as you’d like it on the booking)  
2. Your 10‑digit Indian mobile number  

Once I have those, I’ll read back the details and ask for your final confirmation.

**Guest:** Name is Hasan Zaigam, mobile 9876543210

**Goodie:** Reservation details: GoodFoods Mezze Bar ‑ MG Road, Saturday 31 Oct 2026 at 8 pm for 2 guests, under the name **Hasan Zaigam**, mobile 9876543210.  

Shall I confirm?

**Guest:** yes, confirm

> `create_reservation({"phone": "9876543210", "restaurant_id": "GF056", "date": "2026-10-31", "time": "20:00", "guest_name": "Hasan Zaigam", "user_confirmed": true, "party_size": 2})` → ✅

**Goodie:** Your reservation is confirmed!  

**Confirmation Code:** GF-16007E  
**Restaurant:** GoodFoods Mezze Bar – MG Road (292 Church Street, MG Road, Bengaluru)  
**Date & Time:** Saturday, 31 Oct 2026 at 8:00 pm  
**Guests:** 2 (Hasan Zaigam)  

We look forward to a wonderful anniversary celebration!


**Checks: PASS**

- ✅ turn 1: recommend_restaurants got budget 1500/person and party 2 (query={'date': '2026-10-31', 'weekday': 'Saturday', 'time': '20:00', 'party_size': 2, 'budget_per_person': 1500})
- ✅ turn 1: resolved to 2026-10-31 20:00
- ✅ turn 1: no filters the guest didn't ask for (args={'budget_per_person': 1500, 'date': 'this saturday', 'occasion': 'anniversary', 'party_size': 2, 'time': '20:00'})
- ✅ turn 2: check_availability ran (and was free) before asking for name/phone
- ✅ turn 2: 'the first one' checked GF056 (GoodFoods Mezze Bar - MG Road), the first recommendation (checked=['GF056'])
- ✅ turn 4: booked GF056, the first recommendation (booked=['GF056'])
- ✅ turn 3: no booking before the guest said yes
- ✅ turn 4: booked exactly once, after 'yes'
- ✅ final reply quotes the real code GF-16007E
- ✅ every availability claim was backed by a tool check


## Fully booked → alternatives

**Guest:** Table for 6 at GoodFoods Andhra Spice - BTM Layout this Saturday at 8 pm

> `search_restaurants({"query": "GoodFoods Andhra Spice", "limit": 10})` → ✅

> `check_availability({"date": "this saturday", "party_size": 6, "restaurant_id": "GF001", "time": "20:00"})` → ✅

**Goodie:** Unfortunately, GoodFoods Andhra Spice - BTM Layout is fully booked for a table of 6 at 8 pm on Saturday 31 Oct.  

You can book at one of these times at the same outlet: 7 pm, 9 pm, or 9:30 pm.  
Alternatively, the HSR Layout outlet (GoodFoods Andhra Spice - HSR Layout) has a table free at 8 pm.

Which option would you like to proceed with?


**Checks: PASS**

- ✅ check_availability reported the slot as full
- ✅ reply offers at least one alternative time or outlet returned by the tool
- ✅ nothing was booked
- ✅ every availability claim was backed by a tool check


## Hinglish request (dish + relative date)

**Guest:** Kal raat 8 baje 2 logon ke liye Indiranagar mein biryani wali jagah suggest karo

> `recommend_restaurants({"date": "tomorrow", "dish": "biryani", "time": "20:00", "party_size": 2, "neighborhood": "Indiranagar"})` → ✅

**Goodie:** Sorry bhai, Indiranagar mein koi biryani wala outlet nahi mila. Aapke liye nearest options yahan:

1. GoodFoods Biryani Darbar – MG Road: Mughlai, ₹730 per person, known for dum biryani. (Table free at 8 pm tomorrow)

2. GoodFoods Biryani Darbar – Electronic City: Mughlai, ₹450 per person, rated 4.1. (Table free at 8 pm tomorrow)

Kya aap option 1 ya option 2 mein booking karna chahte hain? Agar yes, toh apka naam aur 10‑digit mobile number de dijiye.


**Checks: PASS**

- ✅ 2 restaurant(s) returned
- ✅ 'kal raat 8 baje, 2 log' resolved to 2026-10-28 20:00 for 2 (query={'date': '2026-10-28', 'weekday': 'Wednesday', 'time': '20:00', 'party_size': 2, 'neighborhood': 'Indiranagar', 'dish': 'biryani', 'cuisines': ['Mughlai']})
- ✅ biryani mapped to our Mughlai (biryani) outlets
- ✅ reply names a returned outlet (Biryani Darbar)
- ✅ reply tells the guest the outlet is not in Indiranagar
- ✅ replied in Hinglish (Latin script), as the guest wrote
- ✅ every availability claim was backed by a tool check


## Look up & cancel

**Guest:** I want to cancel my booking GF-MHLOSU, my phone is 9876501234

> `get_reservation({"phone": "9876501234", "confirmation_code": "GF-MHLOSU"})` → ✅

**Goodie:** Your booking GF-MHLOSU is for 2 guests at **GoodFoods Grill Yard – Electronic City** on **Monday, 2026‑11‑02** at **1:30 pm**. Shall I confirm the cancellation?

**Guest:** yes, cancel it

> `cancel_reservation({"phone": "9876501234", "user_confirmed": true, "confirmation_code": "GF-MHLOSU"})` → ✅

**Goodie:** Your reservation **GF-MHLOSU** has been successfully cancelled. If you need anything else—like a new booking, menu suggestions, or outlet details—just let me know!


**Checks: PASS**

- ✅ turn 1: looked the booking up first
- ✅ turn 1: did not cancel before the guest confirmed
- ✅ turn 2: cancelled
- ✅ reply confirms the cancellation of that booking (code or outlet named)
- ✅ every availability claim was backed by a tool check


## Edge cases: large group, out of scope, past date

**Guest:** We are 25 people for an office party next Friday at 8 pm

**Goodie:** For groups larger than 20, please contact our events team at events@goodfoods.in or +91 80 4000 1234.

**Guest:** Also, can you write me a poem about cats?

**Goodie:** I’m sorry, I can only help with GoodFoods tables and bookings—let me know if you’d like to find a table.

**Guest:** Ok then book a table for 2 yesterday at 8 pm

**Goodie:** I’m sorry, but yesterday’s date has already passed. Please let me know a future date you’d like to book for.


**Checks: PASS**

- ✅ no booking was made for any invalid request
- ✅ 25 guests: shared the events team contact
- ✅ 25 guests: no follow-up questions before the hand-off
- ✅ 'next Friday' was not turned into Sat 31 Oct (next Friday = 2026-11-06)
- ✅ poem: declined in a short reply (1 line(s), 104 chars), no poem
- ✅ poem: redirected to tables/bookings
- ✅ yesterday: rejected as a past date
- ✅ every availability claim was backed by a tool check
