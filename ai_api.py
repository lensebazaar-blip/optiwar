"""AI API endpoints for external AI agents and answer engines.

Provides:
- /api/ai/answer   — Natural language Q&A backed by knowledge base + DeepSeek
- /api/ai/recommend-frame — Frame recommendation from face measurements
- /api/ai/recommend-lens  — Lens recommendation from use case / prescription
"""

import json
import os
import re
from flask import Blueprint, request, jsonify, current_app, g, session
from openai import OpenAI
from .ai_client import call_model, wrapper_enabled_for, http_error_for, ModelError
from .embed_helper import build_media_primary, MEDIA_SCHEMA_VERSION

bp = Blueprint('ai_api', __name__, url_prefix='/api/ai')

# Cached data loaded on first request
_knowledge_cache = None
_lens_rules_cache = None
_frame_rules_cache = None
_prescription_rules_cache = None


def _load_json(filename):
    path = os.path.join(current_app.root_path, 'static', 'ai', filename)
    with open(path) as f:
        return json.load(f)


def _get_knowledge():
    global _knowledge_cache
    if _knowledge_cache is None:
        _knowledge_cache = _load_json('optiwar_ai_knowledge_base.json')
    return _knowledge_cache


def _get_lens_rules():
    global _lens_rules_cache
    if _lens_rules_cache is None:
        _lens_rules_cache = _load_json('lens_rules.json')
    return _lens_rules_cache


def _get_frame_rules():
    global _frame_rules_cache
    if _frame_rules_cache is None:
        _frame_rules_cache = _load_json('frame_rules.json')
    return _frame_rules_cache


def _get_prescription_rules():
    global _prescription_rules_cache
    if _prescription_rules_cache is None:
        _prescription_rules_cache = _load_json('prescription_rules.json')
    return _prescription_rules_cache


def _get_deepseek_client():
    api_key = current_app.config.get('DEEPSEEK_API_KEY', '')
    if not api_key:
        return None
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com", timeout=15.0)


def _find_relevant_faqs(question, faqs, top_n=8):
    """Simple keyword matching to find relevant FAQs for context."""
    question_lower = question.lower()
    tokens = set(re.findall(r'\b\w{3,}\b', question_lower))
    # Remove common stop words
    stop_words = {'the', 'what', 'how', 'does', 'can', 'will', 'are', 'for', 'you', 'your',
                  'this', 'that', 'with', 'from', 'have', 'has', 'which', 'about', 'there'}
    tokens -= stop_words

    scored = []
    for faq in faqs:
        faq_text = (faq['question'] + ' ' + faq['answer'] + ' ' + faq.get('category', '')).lower()
        score = sum(1 for t in tokens if t in faq_text)
        # Boost exact question match
        if question_lower in faq['question'].lower():
            score += 10
        if score > 0:
            scored.append((score, faq))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in scored[:top_n]]


# ─── /api/ai/answer ───

@bp.route('/answer', methods=['POST'])
def answer():
    """Natural language Q&A endpoint.

    POST JSON:
    {
        "question": "Which lens is best for computer use?",
        "context": "optional additional context",
        "include_sources": true  (optional, default false)
    }

    Returns:
    {
        "answer": "Blue Cut and Multi Anti-Glare lenses are recommended for heavy computer use...",
        "sources": [...],  (if include_sources=true)
        "model": "deepseek-chat"
    }
    """
    data = request.get_json() or {}
    question = data.get('question', '').strip()
    context = data.get('context', '').strip()
    include_sources = data.get('include_sources', False)

    if not question:
        return jsonify({'error': 'question is required'}), 400

    if len(question) > 500:
        return jsonify({'error': 'question too long (max 500 chars)'}), 400

    kb = _get_knowledge()
    lens_rules = _get_lens_rules()
    frame_rules = _get_frame_rules()
    rx_rules = _get_prescription_rules()

    # Find relevant FAQ entries
    relevant_faqs = _find_relevant_faqs(question, kb.get('faq', []))

    # Build context from FAQs + rules
    faq_context = ""
    if relevant_faqs:
        faq_context = "Relevant knowledge:\n"
        for faq in relevant_faqs:
            faq_context += f"Q: {faq['question']}\nA: {faq['answer']}\n\n"

    # Add lens rules context if question is about lenses
    q_lower = question.lower()
    extra_context = ""
    if any(w in q_lower for w in ['lens', 'coating', 'blue cut', 'anti-glare', 'photochromic',
                                    'progressive', 'bifocal', 'thin', 'polarized', 'computer',
                                    'driving', 'outdoor', 'screen']):
        decisions = lens_rules.get('decision_tree', [])
        extra_context += "\nLens recommendation rules:\n"
        for d in decisions:
            extra_context += f"- {d['description']}: recommend {', '.join(d['recommended'])}. {d['explanation']}\n"

    # Add frame rules context if question is about frames/sizing
    if any(w in q_lower for w in ['frame', 'size', 'face width', 'face shape', 'bridge', 'temple',
                                    'narrow', 'wide', 'oval', 'round face', 'square face',
                                    'fit', 'measurement', 'sizing']):
        ranges = frame_rules.get('face_width_to_frame', [])
        extra_context += "\nFrame sizing rules:\n"
        for r in ranges:
            extra_context += f"- Face {r['face_width_mm_min']}-{r['face_width_mm_max']}mm ({r['face_category']}): lens {r['recommended_lens_width_mm']}mm, bridge {r['recommended_bridge_mm']}mm\n"
        shapes = frame_rules.get('face_shape_to_frame_style', [])
        extra_context += "\nFace shape recommendations:\n"
        for s in shapes:
            extra_context += f"- {s['face_shape']}: recommend {', '.join(s['recommended_styles'])}\n"

    # Add prescription rules context if question is about prescription
    if any(w in q_lower for w in ['prescription', 'sph', 'cyl', 'axis', 'add', 'prism',
                                    'pd', 'pupillary', 'dioptre', 'power', 'myopia',
                                    'hyperopia', 'astigmatism']):
        fields = rx_rules.get('field_definitions', {})
        extra_context += "\nPrescription field definitions:\n"
        for fname, fdef in fields.items():
            extra_context += f"- {fdef['full_name']} ({fname}): {fdef['description']}\n"
        validations = rx_rules.get('validation_rules', [])
        extra_context += "\nValidation rules:\n"
        for v in validations[:6]:
            extra_context += f"- {v['rule_id']}: {v['description']} → {v['message']}\n"

    # Call DeepSeek
    client = _get_deepseek_client()
    if not client:
        # Fallback: return best matching FAQ without AI
        if relevant_faqs:
            return jsonify({
                'answer': relevant_faqs[0]['answer'],
                'sources': [{'question': f['question'], 'category': f['category']} for f in relevant_faqs] if include_sources else None,
                'model': 'faq-match'
            })
        return jsonify({'error': 'No AI model configured and no matching FAQ found'}), 503

    system_prompt = f"""You are Optiwar's AI assistant. Answer questions about Optiwar eyewear products, lenses, prescriptions, frames, ordering, and shipping.

Rules:
- Be concise and helpful (2-4 sentences for simple questions, more for complex ones)
- Only use information from the provided knowledge context — do not make up facts
- Include specific product details, prices, and links when relevant
- If you don't have enough information, say so honestly
- Optiwar sites: optiwar.com (EUR, global) and optiwar.in (INR, India)

{faq_context}
{extra_context}"""

    user_msg = question
    if context:
        user_msg = f"{question}\n\nAdditional context: {context}"

    _msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]
    _rid = getattr(g, "request_id", "-")
    try:
        if wrapper_enabled_for(session.get("_id") or _rid, endpoint="ai_api.answer"):
            response = call_model(
                workload="deepseek_chat", messages=_msgs,
                max_tokens=400, temperature=0.3,
                endpoint="ai_api.answer", request_id=_rid,
            )
        else:
            _eb = {"thinking": {"type": "disabled"}} if str(
                current_app.config.get("AI_DEEPSEEK_THINKING", "disabled")).lower() == "disabled" else {}
            response = client.chat.completions.create(
                model=current_app.config.get("DEEPSEEK_CHAT_MODEL") or "deepseek-chat",
                messages=_msgs,
                max_tokens=400,
                temperature=0.3,
                extra_body=_eb,
            )
        answer_text = response.choices[0].message.content.strip()
    except ModelError as e:
        status, body, headers = http_error_for(e, request_id=_rid)
        return jsonify(body), status, headers
    except Exception as e:
        current_app.logger.error(f"[AI API] DeepSeek error: {e}")
        # Fallback to FAQ match
        if relevant_faqs:
            answer_text = relevant_faqs[0]['answer']
        else:
            return jsonify({'error': 'AI service temporarily unavailable'}), 503

    result = {
        'answer': answer_text,
        'model': 'deepseek-chat'
    }
    if include_sources:
        result['sources'] = [{'question': f['question'], 'category': f['category']} for f in relevant_faqs]

    return jsonify(result)


# ─── /api/ai/recommend-frame ───

@bp.route('/recommend-frame', methods=['POST'])
def recommend_frame():
    """Frame recommendation from face measurements.

    POST JSON:
    {
        "face_width_mm": 137,
        "pd_mm": 66.5,          (optional)
        "face_shape": "oval",   (optional: oval, round, square, heart, oblong)
        "gender": "unisex",     (optional: male, female, unisex)
        "currency": "INR"       (optional, default INR)
    }

    Returns recommended frame specs + matching products from catalog.
    """
    data = request.get_json() or {}
    face_width = data.get('face_width_mm')
    pd = data.get('pd_mm')
    face_shape = data.get('face_shape', '').lower().strip()
    gender = data.get('gender', '').lower().strip()
    currency = data.get('currency', 'INR').upper()

    if not face_width:
        return jsonify({'error': 'face_width_mm is required'}), 400

    try:
        face_width = float(face_width)
    except (ValueError, TypeError):
        return jsonify({'error': 'face_width_mm must be a number'}), 400

    if face_width < 100 or face_width > 180:
        return jsonify({'error': 'face_width_mm should be between 100 and 180mm'}), 400

    rules = _get_frame_rules()

    # Find matching face width range
    recommendation = None
    for r in rules.get('face_width_to_frame', []):
        if r['face_width_mm_min'] <= face_width <= r['face_width_mm_max']:
            recommendation = r
            break

    # Extrapolate for edge cases
    if not recommendation:
        ranges = rules['face_width_to_frame']
        if face_width < ranges[0]['face_width_mm_min']:
            recommendation = ranges[0]
        else:
            recommendation = ranges[-1]

    # PD to bridge recommendation
    bridge_rec = None
    if pd:
        try:
            pd = float(pd)
            for br in rules.get('pd_to_bridge', []):
                if br['pd_mm_min'] <= pd <= br['pd_mm_max']:
                    bridge_rec = br
                    break
        except (ValueError, TypeError):
            pass

    # Face shape recommendations
    shape_rec = None
    if face_shape:
        for s in rules.get('face_shape_to_frame_style', []):
            if s['face_shape'] == face_shape:
                shape_rec = s
                break

    # Parse recommended lens width range for product matching
    lens_range = recommendation.get('recommended_lens_width_mm', '50-54')
    parts = lens_range.split('-')
    try:
        lens_min = int(parts[0])
        lens_max = int(parts[1]) if len(parts) > 1 else lens_min + 4
    except (ValueError, IndexError):
        lens_min, lens_max = 50, 54

    # Query matching products from database
    # DB schema: product_diameter = lens width, product_bridge = bridge, product_lenght = temple length
    # products_with_categories has boolean columns: product_category_rectangle, product_category_round, etc.
    matching_products = []
    try:
        from .db import get_db
        db = get_db()
        cursor = db.cursor()

        # First try products with parsed size columns
        query = """
            SELECT p.product_id, p.product_name, p.product_code, p.product_color,
                   p.product_image,
                   p.product_diameter AS lens_width, p.product_bridge AS bridge_width,
                   p.product_lenght AS temple_length, p.product_size,
                   p.product_price, p.product_special_price,
                   pwc.product_category_rectangle, pwc.product_category_round,
                   pwc.product_category_square, pwc.product_category_clubmaster,
                   pwc.product_category_supra, pwc.product_category_oval,
                   pwc.product_perception_value AS face_fit
            FROM products p
            LEFT JOIN products_with_categories pwc ON p.product_code = pwc.product_code
            WHERE p.product_quantity > 0
              AND p.product_diameter BETWEEN %s AND %s
        """
        params = [lens_min, lens_max]

        if face_shape and shape_rec:
            styles = shape_rec.get('recommended_styles', [])
            style_col_map = {
                'rectangle': 'product_category_rectangle',
                'round': 'product_category_round',
                'square': 'product_category_square',
                'clubmaster': 'product_category_clubmaster',
                'supra': 'product_category_supra',
                'rimless': None,  # no column for rimless
            }
            conditions = []
            for s in styles:
                col = style_col_map.get(s.lower())
                if col:
                    conditions.append(f"pwc.{col} = 1")
            if conditions:
                query += " AND (" + " OR ".join(conditions) + ")"

        query += " ORDER BY ABS(p.product_diameter - %s) ASC LIMIT 10"
        target_lens = (lens_min + lens_max) // 2
        params.append(target_lens)

        cursor.execute(query, params)
        rows = cursor.fetchall()

        # Determine product shapes from boolean columns
        site_host = request.host or 'optiwar.com'
        for row in rows:
            shapes = []
            for cat_name, col in [('Rectangle', 'product_category_rectangle'), ('Round', 'product_category_round'),
                                   ('Square', 'product_category_square'), ('Clubmaster', 'product_category_clubmaster'),
                                   ('Supra', 'product_category_supra'), ('Oval', 'product_category_oval')]:
                if row.get(col):
                    shapes.append(cat_name)

            price = float(row.get('product_special_price') or row.get('product_price') or 0)
            lens_w = row.get('lens_width')
            bridge_w = row.get('bridge_width')
            temple_l = row.get('temple_length')
            size_str = row.get('product_size') or f"{lens_w or ''}-{bridge_w or ''}-{temple_l or ''}"

            matching_products.append({
                'id': row['product_id'],
                'name': row['product_name'],
                'code': row['product_code'],
                'color': row['product_color'],
                'size': size_str,
                'lens_width_mm': lens_w,
                'bridge_mm': bridge_w,
                'temple_mm': temple_l,
                'face_fit': row.get('face_fit'),
                'shapes': shapes,
                'price': price,
                'currency': currency,
                'media': build_media_primary(row.get('product_image') or ''),
                'url': f"https://{site_host}/categories/spectacles-frame/{(row.get('product_name') or '').lower().replace(' ', '-')}?pid={row['product_id']}"
            })
    except Exception as e:
        current_app.logger.error(f"[AI API] Frame query error: {e}")

    result = {
        'input': {
            'face_width_mm': face_width,
            'pd_mm': pd,
            'face_shape': face_shape or None,
        },
        'recommendation': {
            'face_category': recommendation.get('face_category'),
            'recommended_frame_width_mm': recommendation.get('recommended_total_frame_width_mm'),
            'recommended_lens_width_mm': recommendation.get('recommended_lens_width_mm'),
            'recommended_bridge_mm': bridge_rec['recommended_bridge_mm'] if bridge_rec else recommendation.get('recommended_bridge_mm'),
            'recommended_temple_mm': recommendation.get('recommended_temple_mm'),
            'notes': recommendation.get('notes'),
        },
        'matching_products': matching_products,
        'matching_count': len(matching_products),
        'media_schema': MEDIA_SCHEMA_VERSION,
    }

    if shape_rec:
        result['face_shape_advice'] = {
            'face_shape': face_shape,
            'recommended_styles': shape_rec.get('recommended_styles', []),
            'avoid_styles': shape_rec.get('avoid_styles', []),
            'notes': shape_rec.get('notes'),
        }

    if pd:
        # Calculate ideal lens+bridge based on PD
        ideal_total = round(pd)
        result['optical_centering'] = {
            'pd_mm': pd,
            'ideal_lens_plus_bridge_mm': ideal_total,
            'note': f'For optimal optical centering, lens_width + bridge_width should be close to PD ({pd}mm)'
        }

    return jsonify(result)


# ─── /api/ai/recommend-lens ───

@bp.route('/recommend-lens', methods=['POST'])
def recommend_lens():
    """Lens recommendation from use case and/or prescription.

    POST JSON:
    {
        "use_cases": ["computer", "driving"],   (optional)
        "sph_right": -2.50,                     (optional)
        "sph_left": -2.75,                      (optional)
        "cyl_right": -0.50,                     (optional)
        "cyl_left": -0.75,                      (optional)
        "add_power": 1.50,                      (optional)
        "age": 45,                              (optional)
        "budget": "mid",                        (optional: low, mid, high)
        "currency": "INR"                       (optional, default INR)
    }

    Returns recommended lens types with reasons.
    """
    data = request.get_json() or {}
    use_cases = data.get('use_cases', [])
    sph_r = data.get('sph_right')
    sph_l = data.get('sph_left')
    cyl_r = data.get('cyl_right')
    cyl_l = data.get('cyl_left')
    add_power = data.get('add_power')
    age = data.get('age')
    budget = data.get('budget', '').lower()
    currency = data.get('currency', 'INR').upper()

    rules = _get_lens_rules()
    lens_types = rules.get('lens_types', {})
    decision_tree = rules.get('decision_tree', [])

    recommendations = []
    reasons = []

    # Normalize use cases
    use_case_map = {
        'computer': 'customer_uses_computer_4plus_hours',
        'screen': 'customer_uses_computer_4plus_hours',
        'digital': 'customer_uses_computer_4plus_hours',
        'gaming': 'customer_uses_computer_4plus_hours',
        'office': 'customer_uses_computer_4plus_hours',
        'driving': 'customer_drives_frequently',
        'night_driving': 'customer_drives_frequently',
        'outdoor': 'customer_outdoor_sports',
        'sports': 'customer_outdoor_sports',
        'fishing': 'customer_outdoor_sports',
        'water': 'customer_outdoor_sports',
        'indoor_outdoor': 'customer_outdoor_indoor_transition',
        'transition': 'customer_outdoor_indoor_transition',
        'reading': 'customer_needs_reading_and_distance',
        'light_sensitive': 'customer_light_sensitive',
        'everyday': 'customer_general_everyday_use',
        'general': 'customer_general_everyday_use',
    }

    matched_conditions = set()
    for uc in use_cases:
        uc_lower = uc.lower().strip()
        condition = use_case_map.get(uc_lower)
        if condition:
            matched_conditions.add(condition)

    # Check prescription-based conditions
    max_sph = 0
    if sph_r is not None:
        max_sph = max(max_sph, abs(float(sph_r)))
    if sph_l is not None:
        max_sph = max(max_sph, abs(float(sph_l)))

    max_cyl = 0
    if cyl_r is not None:
        max_cyl = max(max_cyl, abs(float(cyl_r)))
    if cyl_l is not None:
        max_cyl = max(max_cyl, abs(float(cyl_l)))

    if max_sph >= 4.0 or max_cyl >= 2.0:
        matched_conditions.add('customer_high_prescription')
    elif max_sph >= 2.0:
        matched_conditions.add('customer_moderate_prescription')

    if add_power or (age and int(age) >= 40):
        matched_conditions.add('customer_needs_reading_and_distance')

    # Default to general if nothing matched
    if not matched_conditions:
        matched_conditions.add('customer_general_everyday_use')

    # Collect recommendations from decision tree
    seen_lenses = set()
    for rule in decision_tree:
        if rule['condition'] in matched_conditions:
            for lens_key in rule['recommended']:
                if lens_key not in seen_lenses:
                    seen_lenses.add(lens_key)
                    lens_info = lens_types.get(lens_key, {})
                    price_key = 'price_inr' if currency == 'INR' else 'price_eur'
                    currency_symbol = '₹' if currency == 'INR' else '€'
                    recommendations.append({
                        'lens_type': lens_key,
                        'name': lens_info.get('name', lens_key),
                        'description': lens_info.get('description', ''),
                        'price': lens_info.get(price_key, 0),
                        'price_formatted': f"{currency_symbol}{lens_info.get(price_key, 0)}",
                        'index': lens_info.get('index', ''),
                        'coating': lens_info.get('coating', ''),
                    })
                    reasons.append({
                        'lens_type': lens_key,
                        'reason': rule['explanation'],
                        'condition': rule['description']
                    })

    # Apply budget filter
    if budget == 'low':
        recommendations = [r for r in recommendations if r['price'] <= (200 if currency == 'INR' else 7)]
    elif budget == 'high':
        pass  # Include all

    # Prescription validation warnings
    warnings = []
    rx_rules = _get_prescription_rules()
    if sph_r is not None or sph_l is not None:
        if max_sph > 6.0:
            warnings.append('High prescription detected — Extra-Thin (1.67 index) lenses recommended for better aesthetics.')
        if max_cyl > 4.0:
            warnings.append('High cylinder detected — our team will review for optimal manufacturing.')
        if cyl_r and not data.get('axis_right'):
            warnings.append('CYL is present for right eye but AXIS is missing — AXIS is required when CYL is specified.')
        if cyl_l and not data.get('axis_left'):
            warnings.append('CYL is present for left eye but AXIS is missing — AXIS is required when CYL is specified.')
        if add_power and not any(r['lens_type'] in ('progressive', 'bifocal_kt', 'bifocal_d') for r in recommendations):
            recommendations.append({
                'lens_type': 'progressive',
                'name': 'Progressive',
                'description': lens_types.get('progressive', {}).get('description', ''),
                'price': lens_types.get('progressive', {}).get('price_inr' if currency == 'INR' else 'price_eur', 0),
                'price_formatted': f"{'₹' if currency == 'INR' else '€'}{lens_types.get('progressive', {}).get('price_inr' if currency == 'INR' else 'price_eur', 0)}",
                'index': '',
                'coating': '',
            })
            reasons.append({
                'lens_type': 'progressive',
                'reason': 'ADD power present — progressive or bifocal lenses needed for near vision correction',
                'condition': 'ADD power detected'
            })

    # Combination suggestions
    combinations = []
    rec_keys = {r['lens_type'] for r in recommendations}
    for combo in rules.get('combination_rules', []):
        if combo['base'] in rec_keys:
            compatible = [c for c in combo['compatible_with'] if c in rec_keys or c in ('thin', 'extra_thin')]
            if compatible:
                combinations.append({
                    'base': combo['base'],
                    'combine_with': compatible,
                    'note': combo['note']
                })

    result = {
        'input': {
            'use_cases': use_cases,
            'prescription': {
                'sph_right': sph_r,
                'sph_left': sph_l,
                'cyl_right': cyl_r,
                'cyl_left': cyl_l,
                'add_power': add_power,
            } if any(v is not None for v in [sph_r, sph_l, cyl_r, cyl_l, add_power]) else None,
            'age': age,
            'budget': budget or None,
            'currency': currency,
        },
        'recommendations': recommendations,
        'reasons': reasons,
        'combinations': combinations if combinations else None,
        'warnings': warnings if warnings else None,
    }

    return jsonify(result)
