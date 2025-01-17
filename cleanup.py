import getpass
import sys
import time
from datetime import datetime, timedelta

import params
import util
import ollama_lib
from db.database import get_db
from db.models import Rule
from falcon import FalconClient, process_gmail_dic


def lower_strip_clean(string):
    if string is None:
        return ''
    return util.clean_text(string).lower()


def evaluate_clause(clause, sender, subject, text, labels, tags, timediff, snippet):
    try:
        """
            variables needed in args for eval() to work
        """

        sender = lower_strip_clean(sender)
        sender_alias = sender.split('@')[0]
        sender_domain = sender.split('@')[1]

        labels = {i.lower() for i in labels}
        tags = {i.lower() for i in tags}

        subject = lower_strip_clean(subject)
        snippet = lower_strip_clean(snippet)
        text = lower_strip_clean(text)
        subject_snippet = f'{subject} {snippet}'
        content = f'{subject} {snippet} {text}'

        minute = 60
        hour = 60 * minute
        day = 24 * hour
        week = 7 * day
        month = 30 * day
        year = 365 * day

        locals_dict = locals()

        return eval(clause, locals_dict, {})
    except Exception as e:
        util.error(f'{sender}:[{e}]')
        return False


def get_mail(falcon_client, mail_id):
    return falcon_client.gmail.get_mail(mail_id)


def consolidate(falcon_client, main_query):
    query = f'in:spam'
    mails = falcon_client.gmail.list_mails(query=query, max_pages=10000)
    for index, mail in enumerate(mails, 0):
        mail_id = mail['id']
        falcon_client.gmail.move_to_trash(mail_id)

        time.sleep(0.5)


def get_label_names(mail_processed, label_id_to_name_mapping):
    return {label_id_to_name_mapping[i] for i in mail_processed['LabelIds']}


def should_delete_email(mail_processed, blacklist_rules, whitelist_rules, label_id_to_name_mapping):
    curr_time = int(time.time())

    sender = lower_strip_clean(mail_processed['Sender'])
    subject = mail_processed['Subject']
    text = mail_processed['Text']
    snippet = mail_processed['Snippet']
    timediff = curr_time - int(mail_processed['DateTime'].timestamp())
    labels = get_label_names(mail_processed, label_id_to_name_mapping)
    tags = set()

    if mail_processed['Unsubscribe'] is not None:
        tags.add('unsubscribe')

    for q in whitelist_rules:
        if evaluate_clause(q, sender, subject, text, labels, tags, timediff, snippet):
            util.log(f'Do not delete since [{q}] evaluates to True.')
            return False

    for q in blacklist_rules:
        if evaluate_clause(q, sender, subject, text, labels, tags, timediff, snippet):
            util.log(f'Delete since [{q}] evaluates to True.')
            return True

    return False


def process_labelling(mail_processed, label_rules, add_labels, remove_labels, label_id_to_name_mapping):
    curr_time = int(time.time())

    sender = mail_processed['Sender']
    subject = mail_processed['Subject']
    text = mail_processed['Text']
    snippet = mail_processed['Snippet']
    timediff = curr_time - int(mail_processed['DateTime'].timestamp())
    labels = get_label_names(mail_processed, label_id_to_name_mapping)
    tags = set()

    if mail_processed['Unsubscribe'] is not None:
        tags.add('unsubscribe')

    for q, label_out, args in label_rules:
        label_out = label_out.upper().strip()

        label_op_type = label_out[0]
        label_name = label_out[1:]

        if args is None:
            args = set()
        else:
            args = set(args.split(','))

        if evaluate_clause(q, sender, subject, text, labels, tags, timediff, snippet):
            if label_op_type == '+':
                if label_name not in labels:
                    util.log(f'Add label [{label_name}] since [{q}] evaluates to True.')
                    labels.add(label_name)
                    add_labels.append(label_name)
            elif label_op_type == '-':
                if label_name in labels:
                    util.log(f'Remove label [{label_name}] since [{q}] evaluates to True.')
                    labels.remove(label_name)
                    remove_labels.append(label_name)
            else:
                raise Exception(f'Invalid rule out [{label_out}].')

            if 'skip_others' in args:
                util.log('Skipping processing other labelling rules.')
                break

def apply_ai_labels(mail_processed, ai_labels, add_labels, remove_labels, label_id_to_name_mapping):
    email_labels = get_label_names(mail_processed, label_id_to_name_mapping)
    
    out_labels, model_name = ollama_lib.process_email(mail_processed, ai_labels)

    prev_ai_labels = [i for i in email_labels if i.startswith(f'AI/{model_name}'.upper())]
    new_ai_labels = [f'AI/{model_name}/{label}'.upper() for label in out_labels]

    for label in new_ai_labels:
        if label not in email_labels:
            add_labels.append(label)

    for label in prev_ai_labels:
        if label not in new_ai_labels:
            remove_labels.append(label)

    
def cleanup(email, main_query, num_days, key):
    util.log(f'Cleanup triggered for {email} - {main_query}.')

    db = get_db()

    def get_query(rule_type):
        return (Rule.type.like(f'{rule_type}%')) & ((Rule.apply_to == 'all') | (Rule.apply_to.like(f'%+({email})%')))

    blacklist_rules = {i.query for i in db.session.query(Rule).filter(get_query('blacklist')).all()}

    whitelist_rules = {i.query for i in db.session.query(Rule).filter(get_query('whitelist')).all()}

    ai_labels = ollama_lib.get_ai_labels()

    # For safety, I have kept this hard-coded
    whitelist_rules.add("'starred' in labels")

    label_rules = [(i.query, i.type.split(':')[1], i.args) for i in
                   db.session.query(Rule).filter(get_query('label')).order_by(Rule.order).all()]

    util.log(f'Blacklist: [{blacklist_rules}]')
    util.log(f'Labelling rules: [{label_rules}].')

    falcon_client = FalconClient(email=email, key=key)

    get_query = main_query
    if get_query is None:
        get_query = ''

    after = datetime.now() - timedelta(days=num_days)

    get_query += f" after:{after.strftime('%Y/%m/%d')}"
    get_query += ' -in:sent'
    get_query += ' -in:trash'
    get_query.strip()

    mails = falcon_client.gmail.list_mails(query=get_query, max_pages=10000)

    labels_info = falcon_client.gmail.list_labels()["labels"]

    created_label_names = {label["name"]: label["id"] for label in labels_info}
    created_label_ids = {label["id"]: label["name"] for label in labels_info}

    for mail in mails:
        mail_id = mail["id"]

        mail_full = get_mail(falcon_client, mail_id)
        mail_processed = process_gmail_dic(mail_full)

        # --------------- code to dump
        # mail_processed['DateTime'] = int(mail_processed['DateTime'].timestamp())
        # mail_processed['Email'] = email
        # util.save_mail_to_cache(mail_processed)
        # continue
        # -----------------

        move_to_trash = should_delete_email(
            mail_processed,
            blacklist_rules,
            whitelist_rules,
            created_label_ids
        )

        if not move_to_trash:
            add_label_names = []
            remove_label_names = []

            process_labelling(
                mail_processed,
                label_rules,
                add_label_names,
                remove_label_names,
                created_label_ids
            )

            if use_llm:
                apply_ai_labels(mail_processed, ai_labels, add_label_names, remove_label_names, created_label_ids)

            existing_label_ids = mail_processed['LabelIds']

            add_label_ids = []
            for label_name in add_label_names:
                prev_node = ''
                for label_node in label_name.split('/'):
                    if len(prev_node) > 0:
                        label_node = f'{prev_node}/{label_node}'

                    label_id = created_label_names.get(label_node, None)
                    if label_id is None:
                        util.log(f'Label [{label_node}] not found, creating it.')
                        label_id = falcon_client.gmail.create_label(label_node)['id']
                        created_label_names[label_node] = label_id
                        created_label_ids[label_id] = label_node
                    
                    prev_node = label_node

                add_label_ids.append(label_id)

            remove_label_ids = [created_label_names[i] for i in remove_label_names if
                                created_label_names[i] in existing_label_ids]

            if len(add_label_ids) > 0 or len(remove_label_ids) > 0:
                falcon_client.gmail.add_remove_labels(mail_id, add_label_ids, remove_label_ids)
                for label_name in remove_label_ids:
                    mail_full['labelIds'].remove(label_name)
                for label_name in add_label_ids:
                    mail_full['labelIds'].append(label_name)

            move_to_trash = should_delete_email(
                mail_processed,
                blacklist_rules,
                whitelist_rules,
                created_label_ids
            )

        if move_to_trash:
            falcon_client.gmail.move_to_trash(mail_id)

        time.sleep(0.5)

    consolidate(falcon_client, main_query)


if __name__ == "__main__":
    try:
        num_days = int(sys.argv[1]) if len(sys.argv) > 1 else -1
        if num_days == -1:
            num_days = 2

        key = sys.argv[2] if len(sys.argv) > 2 else None
        if key is None or key == "#":
            key = getpass.getpass("Please provide secret key: ")

        use_llm = len(sys.argv) > 3 and sys.argv[3] == "1"

        util.log(f"Running cleanup on emails in last [{num_days}] days.")

        for em in list(params.emails):
            cleanup(email=em, main_query=params.emails[em], num_days=num_days, key=key)

    except Exception as exp:
        util.error(exp)
