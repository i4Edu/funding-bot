from __future__ import annotations

from pathlib import Path

from funding_bot import _default_http_json_client


def test_common_fixtures_seed_donors_tasks_connectors_and_documents(
    funding_bot,
    donors,
    tasks,
    connectors,
    documents,
) -> None:
    assert len(donors) == 2
    assert {donor['email'] for donor in donors} == {'unicef@example.org', 'acme@example.org'}
    assert len(tasks) == 2
    assert {task['title'] for task in tasks} == {'Collect donor documents', 'Review grant budget'}
    assert len(connectors) == 4
    assert all(getattr(connector, 'transport', None) == 'demo' for connector in connectors)
    assert len(documents) == 2
    assert Path(documents[0]['pdf']).exists()
    assert Path(documents[0]['docx']).exists()
    assert Path(documents[1]['pdf']).exists()
    assert funding_bot.connection.execute('SELECT COUNT(*) FROM documents').fetchone()[0] >= 2


def test_factory_fixtures_accept_overrides(donor_factory, task_factory, document_factory) -> None:
    donor = donor_factory(name='Strategic Partner', segment='institutional', locale='bn')
    task = task_factory(title='Prepare translated brief', assigned_to='auditor', due_date='2026-09-02')
    document = document_factory(kind='translated_brief', formats=('pdf',))

    assert donor['name'] == 'Strategic Partner'
    assert donor['segment'] == 'institutional'
    assert task['assigned_to'] == 'auditor'
    assert task['due_date'] == '2026-09-02'
    assert document['kind'] == 'translated_brief'
    assert Path(document['pdf']).exists()


def test_db_transaction_fixture_rolls_back_writes(db_transaction) -> None:
    created = db_transaction.bot.create_task(title='Transactional task', assigned_to='staff')

    assert created['title'] == 'Transactional task'
    assert db_transaction.blocked_commits >= 1
    assert db_transaction.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1

    db_transaction.rollback()

    assert db_transaction.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0


def test_api_mock_fixture_patches_default_http_session(api_mocks) -> None:
    api_mocks.register_json(
        'POST',
        'https://api.example.test/opportunities',
        {'rows': [{'title': 'Education Innovation Grant'}]},
    )

    payload = _default_http_json_client(
        'https://api.example.test/opportunities',
        {'keywords': ['education']},
        {'access_token': 'demo-token'},
    )

    assert payload['rows'][0]['title'] == 'Education Innovation Grant'
    assert api_mocks.calls[0]['headers']['Authorization'] == 'Bearer ' + 'demo-token'


def test_redis_and_celery_mock_fixtures_capture_common_operations(
    redis_mock,
    celery_task_mock,
    celery_app_mock,
) -> None:
    assert redis_mock.ping() is True
    redis_mock.set('queue_depth', 1)
    assert redis_mock.incr('queue_depth') == 2
    assert redis_mock.get('queue_depth') == 2

    delayed = celery_task_mock.delay(keywords=['education'])
    queued = celery_task_mock.apply_async(kwargs={'keywords': ['education']}, queue='funding-bot')
    sent = celery_app_mock.send_task('funding_bot.discover', kwargs={'keywords': ['education']})

    assert delayed.id.startswith('funding_bot.discover-')
    assert queued.payload['options']['queue'] == 'funding-bot'
    assert celery_task_mock.calls[0]['kwargs']['keywords'] == ['education']
    assert sent.payload['name'] == 'funding_bot.discover'
    assert celery_app_mock.control.inspect().ping()['worker-1']['ok'] == 'pong'
