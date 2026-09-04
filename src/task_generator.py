import os

def generate_task_set():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(current_dir, "..", "output")
    
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    output_path = os.path.join(output_dir, "task_set.json")
    
    json_content = """{
    "periodic": {
        "p1": {"r": 1, "p": 8, "e": 4, "d": 4, "w": 15, "preempt": 1},
        "p2": {"r": 2, "p": 8, "e": 2, "d": 4, "w": 14, "preempt": 0},
        "p3": {"r": 3, "p": 8, "e": 2, "d": 4, "w": 10, "preempt": 0},
        "p4": {"r": 1, "p": 12, "e": 4, "d": 4, "w": 8, "preempt": 1},
        "p5": {"r": 2, "p": 12, "e": 3, "d": 5, "w": 6, "preempt": 1},
        "p6": {"r": 3, "p": 12, "e": 1, "d": 5, "w": 8, "preempt": 1},
        "p7": {"r": 1, "p": 6, "e": 1, "d": 6, "w": 12, "preempt": 1}
    }
}"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(json_content)
        
    print(f"成功產出檔案至指定結構位置：{output_path}")

if __name__ == "__main__":
    generate_task_set()