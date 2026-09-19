"""A small Hindi knowledge base for retrieval tests.

Written for testing only - it is NOT the client's document and makes no claim
to be authoritative. It deliberately mixes the Lumpy Skin Disease passages with
neighbours that are easy to confuse with them (another disease that also has a
"टीका", feeding advice that also mentions milk), because retrieval that only
works when the topic is unambiguous is not the retrieval that fails in practice.
"""

LSD_KB: list[tuple[str, str]] = [
    ("intro",
     "लम्पी स्किन डिजीज (LSD) गाय और भैंस में होने वाला एक विषाणुजनित रोग है। इसे गांठदार त्वचा रोग भी कहा जाता है। "
     "यह रोग तेजी से एक पशु से दूसरे पशु में फैलता है और पशुपालकों को आर्थिक नुकसान पहुँचाता है।"),
    ("symptoms",
     "लम्पी रोग के मुख्य लक्षणों में तेज बुखार, शरीर की त्वचा पर जगह-जगह गोल कठोर गांठें, नाक और आँखों से पानी बहना "
     "तथा पैरों में सूजन शामिल हैं। प्रभावित पशु खाना कम कर देता है और दूध का उत्पादन घट जाता है।"),
    ("spread",
     "यह रोग मुख्य रूप से मक्खियों, मच्छरों और किलनी के काटने से फैलता है। बीमार पशु के सीधे संपर्क में आने, "
     "दूषित पानी और चारे से भी संक्रमण हो सकता है।"),
    ("prevention",
     "बचाव के लिए बीमार पशु को तुरंत अन्य पशुओं से अलग रखें। पशुशाला में मक्खी, मच्छर और किलनी की रोकथाम करें, "
     "साफ-सफाई रखें और नए पशु को झुंड में मिलाने से पहले अलग रखकर देखें।"),
    ("vaccination",
     "स्वस्थ पशुओं का टीकाकरण इस रोग से बचाव का सबसे प्रभावी तरीका है। टीका लगवाने के लिए अपने नजदीकी सरकारी "
     "पशु चिकित्सालय या पशु चिकित्सक से संपर्क करें। बीमार पशु को टीका न लगवाएं।"),
    ("milk_meat",
     "लम्पी रोग से प्रभावित पशु का दूध उबालकर ही उपयोग करें। बीमार पशु के मांस के उपयोग से पहले पशु चिकित्सक की "
     "सलाह अवश्य लें।"),
    ("what_to_do",
     "पशु में इस रोग के लक्षण दिखें तो घरेलू उपचार पर निर्भर न रहें। पशु को अलग रखें, उसे छाया में रखें, स्वच्छ पानी दें "
     "और तुरंत नजदीकी सरकारी पशु चिकित्सालय को सूचना दें। इलाज पशु चिकित्सक की देखरेख में ही कराएं।"),
    ("ethnovet",
     "राष्ट्रीय डेयरी विकास बोर्ड द्वारा प्रकाशित पारंपरिक नुस्खों में पान के पत्ते, काली मिर्च, नमक और गुड़ का उल्लेख है। "
     "इन्हें पशु चिकित्सक की सलाह के साथ ही उपयोग करें।"),
    ("mastitis",
     "थनैला रोग में थन में सूजन आ जाती है और दूध में खून या थक्के दिखाई देते हैं। दूध निकालने से पहले थन को साफ करें "
     "और दूध निकालने के बाद थन को कीटाणुनाशक घोल में डुबोएं।"),
    ("fmd",
     "मुँहखुर रोग में पशु के मुँह और खुरों में छाले पड़ जाते हैं और पशु लंगड़ाने लगता है। इस रोग का टीका साल में दो बार "
     "लगवाना चाहिए।"),
    ("feeding",
     "गाभिन पशु को संतुलित आहार दें। हरा चारा, सूखा चारा और खनिज मिश्रण की सही मात्रा से दूध उत्पादन बढ़ता है।"),
    ("deworming",
     "पशुओं को साल में दो बार कृमिनाशक दवा पशु चिकित्सक की सलाह से देनी चाहिए ताकि पेट के कीड़ों से बचाव हो सके।"),
]

# (question, [previous caller turns], key that MUST be among the top results)
ANSWERABLE: list[tuple[str, list[str], str]] = [
    ("लम्पी रोग क्या है?", [], "intro"),
    ("लम्पी स्किन डिजीज क्या होती है", [], "intro"),
    ("लम्पी रोग के लक्षण क्या हैं?", [], "symptoms"),
    ("लम्पी रोग कैसे फैलता है?", [], "spread"),
    ("लम्पी से बचाव कैसे करें", [], "prevention"),
    ("लम्पी का टीका कब लगवाएं", [], "vaccination"),
    ("क्या लम्पी में दूध पी सकते हैं", [], "milk_meat"),
    # Lumps -> the symptom passage is the evidence retrieval can give. Turning "what do I do" into the
    # "keep it apart, call the vet" passage needs semantic (embedding) understanding, which a lexical
    # index cannot supply; the helpline persona carries that escalation rule itself.
    ("मेरी गाय को गांठें हो गई हैं क्या करूं", [], "symptoms"),
    # spelling as a speech recogniser or another document might write it
    ("लंपी रोग के लक्षण", [], "symptoms"),
    ("लम्पि रोग कैसे फैलता है", [], "spread"),
    # synonyms and other names for the disease
    ("गांठदार त्वचा रोग के लक्षण बताइए", [], "symptoms"),
    ("एलएसडी से बचाव", [], "prevention"),
    # Romanised / Hinglish typed queries
    ("lumpy skin disease ke lakshan", [], "symptoms"),
    ("lampi rog kaise failta hai", [], "spread"),
    ("lumpy ka ilaj kya hai", [], "what_to_do"),
    # one word
    ("लम्पी", [], "intro"),
    # inflected forms of the same words
    ("मक्खियों से कैसे बचें", ["लम्पी रोग क्या है"], "prevention"),
    # follow-ups that name no topic of their own
    ("इसका इलाज क्या है?", ["लम्पी रोग के लक्षण क्या हैं"], "what_to_do"),
    ("यह कैसे फैलता है?", ["लम्पी रोग क्या है"], "spread"),
    ("इसके बचाव के तरीके क्या हैं", ["लम्पी रोग के लक्षण क्या हैं"], "prevention"),
    ("टीका कब लगवाना चाहिए", ["लम्पी रोग से बचाव कैसे करें"], "vaccination"),
    # a topic carried across acknowledgements
    ("और बचाव?", ["लम्पी रोग के लक्षण क्या हैं", "इसका इलाज क्या है", "जी हाँ"], "prevention"),
]

# Questions the knowledge base does not answer: must retrieve nothing at all.
UNANSWERABLE: list[str] = [
    "आज मौसम कैसा है",
    "बिजली का बिल कैसे भरें",
    "क्रिकेट मैच का स्कोर क्या है",
    "गेहूं की बुवाई कब करें",
]
