# kuuki-mouse

## usage
```sh
cd py/cert
py cert.py
```


# grpc

run/compile `grpc_remote_control/server.py`

use `grpc_controller.py` as client.


# log

(0827) for the time being. 
but how?
use the acceration on 2-dim directions.
each use kalman filter.


# how to predict the gravity.
it should be 9.8 or 9.9 but how to substract the value?


一个可供参数被学习的代码框架是?
什么vibe coding.
loss如何传递过去.

比如这里就会有一个重力向量和测得加速度向量.
然后还有一个位置参数也就是和 `[0,0,9.8]` 的夹角
姑且还是有一个门限参数,决定是否需要进入平放模式,这个是应该被学习的.
姑且还是有一个门限参数,决定在斜面上是否需要进入平放模式.


姑且不太对的.可能还是要写一下两条管线。
你妈的，自带回环的怎么写啊我日。
这里的感觉可能是凑齐所有可能需要的数据然后写一个model然后让他自己预测。


输出-预测输出-实际输出。
其实应该就是这两个东西的。分布。
我擦。

有一个节点是。
对应了。

采样和分布。张那个样子。



#


简单的,任务.