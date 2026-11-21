import os

from carnatic_bingo_generator import generate_sheet, load_state, next_generation_params, output_filename


def test_first_generation_sheet_number_and_manifest(env_isolated, tmp_state_file, tmp_output_dir):
    entry = generate_sheet(state_path=tmp_state_file, output_dir=tmp_output_dir)
    assert entry["sheet_number"] == 1
    assert entry["filename"] == output_filename(1)
    assert entry["path"].startswith(tmp_output_dir)
    assert os.path.isfile(entry["path"])

    state = load_state(tmp_state_file)
    assert len(state["generated_sheets"]) == 1
    assert state["generated_sheets"][0]["filename"] == "Carnatic_Bingo_Sheet_001.docx"


def test_next_generation_params_increments(env_isolated, tmp_state_file):
    sheet_number, clue_order, mcq_index, state = next_generation_params(state_path=tmp_state_file)
    assert sheet_number == 1
    assert len(clue_order) == 24
    assert mcq_index == 0
    assert state["sheet_number"] == 1
    assert len(state["past_clue_orders"]) == 1


def test_larger_pool_still_draws_exactly_24_clues(tmp_state_file):
    _, clue_order, _, _ = next_generation_params(num_clues=100, state_path=tmp_state_file)
    assert len(clue_order) == len(set(clue_order)) == 24
    assert all(0 <= index < 100 for index in clue_order)
