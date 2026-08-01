from table_miku import knowledge_base
from table_miku.knowledge_seed import (
    CURATED_INTERVIEW_QA,
    SOURCE_LABEL,
    curated_question_count,
)


def test_offline_interview_bank_has_at_least_sixty_original_questions():
    assert curated_question_count() >= 60
    questions = [
        pair["question"]
        for pairs in CURATED_INTERVIEW_QA.values()
        for pair in pairs
    ]
    assert len(questions) == len(set(questions))


def test_every_seed_question_has_structured_interview_answer():
    for topic, pairs in CURATED_INTERVIEW_QA.items():
        assert pairs, topic
        for pair in pairs:
            assert pair["question"]
            assert pair["answer_summary"]
            assert pair["answer_detail"]
            assert pair["key_points"]
            assert pair["pitfalls"]
            assert pair["follow_ups"]
            assert pair["source_label"] == SOURCE_LABEL
            assert pair["question_type"] == "high-frequency"
            assert "一句话结论" in pair["answer"]
            assert "原理拆解" in pair["answer"]
            assert "工程示例" in pair["answer"]
            assert "面试追问" in pair["answer"]


def test_seed_answers_are_question_specific():
    java_pairs = CURATED_INTERVIEW_QA["Java 后端基础"]
    ioc = next(pair for pair in java_pairs if "Spring IoC" in pair["question"])
    hashmap = next(pair for pair in java_pairs if "HashMap 的 put" in pair["question"])
    acid = next(
        pair
        for pair in CURATED_INTERVIEW_QA["数据库原理"]
        if "ACID" in pair["question"]
    )

    assert "容器" in ioc["answer"] and "依赖" in ioc["answer"]
    assert "HashMap" not in ioc["answer"]
    assert "桶" in hashmap["answer"] and "扩容" in hashmap["answer"]
    assert all(word in acid["answer"] for word in ("原子性", "一致性", "隔离性", "持久性"))


def test_fallback_only_reviews_curated_questions():
    java = knowledge_base._fallback_card("Java 后端基础")
    unknown = knowledge_base._fallback_card("自定义学习主题")

    assert len(java["qa_pairs"]) == len(java["review_questions"])
    assert java["qa_pairs"][0]["canonical_key"]
    assert unknown["review_questions"]
    assert unknown["qa_pairs"] == []
