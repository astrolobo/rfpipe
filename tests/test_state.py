import pytest
import rfpipe

class TestClass_create:

    def test_state0(self):
        st = rfpipe.state.State()
        assert st.metadata.atdefaults()

