# P4Runtime Python Client

- **Notice**: ONLY packet in was done in this repo.

- Clients I found:
    - Python: I ref code in [1] for this program. [2] uses the very old version P4runtime.proto but it's OK for simple demo.
    - C++: [3] uses simple_switch(not simple_swithc_grpc) as simulation environment, which use nanomsg for Packet IO(i.e. packet in/out). Currently, Packet IO is transmit with [grpc stream](https://github.com/p4lang/p4runtime/blob/4650de4734376e33357da1662e2635930342c876/proto/p4/v1/p4runtime.proto#L513)
    - Java: [4] Which is too hard for me to understand. @@

- Dependence
    - PI commit: 539e4624f16aac39f8890a6dfb11c65040e735ad
    - grpc and etc.

- References:
    [1] [fabric-p4test](https://github.com/opennetworkinglab/fabric-p4test)
    [2] [P4 tutorials P4runtime](https://github.com/p4lang/tutorials/blob/master/exercises/p4runtime/mycontroller.py)
    [3] [demo_grpc](https://github.com/p4lang/PI/tree/master/proto/demo_grpc)
    [4] [ONOS Soundbound](https://github.com/opennetworkinglab/onos/blob/master/protocols/p4runtime/ctl/src/main/java/org/onosproject/p4runtime/ctl/P4RuntimeControllerImpl.java)